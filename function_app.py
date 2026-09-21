import json
import logging
import os
import re
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import azure.functions as func
import pyodbc


app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)

OBJECT_PATTERN = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$"
)
UNSAFE_SQL_PATTERN = re.compile(r"(;|--|/\*|\*/)")
SWAGGER_FILE = Path(__file__).with_name("swagger.json")


def _get_setting(name: str, default: str | None = None) -> str:
    value = os.getenv(name, default)
    if value is None or not value.strip():
        raise RuntimeError(f"Falta la variable de configuración {name}.")
    return value.strip()


def _get_allowed_objects() -> set[str]:
    configured_objects = _get_setting(
        "SQL_ALLOWED_OBJECTS", "FACTURAS,CLIENTES"
    )
    objects = {
        object_name.strip().upper()
        for object_name in configured_objects.split(",")
        if object_name.strip()
    }
    if not objects or any(not OBJECT_PATTERN.fullmatch(name) for name in objects):
        raise RuntimeError(
            "SQL_ALLOWED_OBJECTS debe contener nombres separados por comas."
        )
    if any("." in name for name in objects):
        raise RuntimeError(
            "SQL_ALLOWED_OBJECTS debe contener nombres sin schema."
        )
    return objects


def _resolve_object_name(object_name: object) -> str:
    if not isinstance(object_name, str) or not object_name.strip():
        raise ValueError("object es obligatorio.")
    requested_object = object_name.strip().upper()
    if "." in requested_object or not OBJECT_PATTERN.fullmatch(requested_object):
        raise ValueError(
            "object debe ser un nombre sin schema, por ejemplo CLIENTES."
        )
    if requested_object not in _get_allowed_objects():
        raise ValueError("El object solicitado no está permitido.")

    schema = _get_setting("SQL_OBJECT_SCHEMA", "dbo")
    if not re.fullmatch(r"^[A-Za-z_][A-Za-z0-9_]*$", schema):
        raise RuntimeError("SQL_OBJECT_SCHEMA contiene un nombre no válido.")
    return f"[{schema}].[{requested_object}]"


def _get_columns() -> str:
    columns = _get_setting("SQL_SELECT_COLUMNS", "*")
    if columns == "*":
        return columns
    if not re.fullmatch(r"[A-Za-z0-9_.,\s\[\]]+", columns):
        raise RuntimeError(
            "SQL_SELECT_COLUMNS contiene caracteres no permitidos."
        )
    return columns


def _get_requested_columns(parameters: object) -> str:
    if parameters is None or parameters == []:
        return _get_columns()
    if not isinstance(parameters, list) or not parameters:
        raise ValueError("parameters debe ser una lista de campos.")
    if any(
        not isinstance(field, str)
        or not re.fullmatch(r"^[A-Za-z_][A-Za-z0-9_]*$", field)
        for field in parameters
    ):
        raise ValueError(
            "parameters solo puede contener nombres de campos válidos."
        )
    return ", ".join(f"[{field}]" for field in parameters)


def _positive_int(value: object, name: str, default: int) -> int:
    if value is None:
        return default
    try:
        parsed = int(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} debe ser un entero positivo.") from exc
    if parsed < 1:
        raise ValueError(f"{name} debe ser un entero positivo.")
    return parsed


def _nonnegative_int(value: object, name: str, default: int) -> int:
    if value is None:
        return default
    try:
        parsed = int(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} debe ser un entero no negativo.") from exc
    if parsed < 0:
        raise ValueError(f"{name} debe ser un entero no negativo.")
    return parsed


def _json_default(value: object) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _request_data(req: func.HttpRequest) -> dict:
    try:
        body = req.get_json()
    except ValueError:
        body = {}
    if body is None:
        return {}
    if not isinstance(body, dict):
        raise ValueError("El cuerpo JSON debe ser un objeto.")
    return body


def _validate_order_by(order_by: object) -> str:
    if not isinstance(order_by, str) or not order_by.strip():
        raise ValueError("order_by es obligatorio para paginar.")
    value = order_by.strip()
    if UNSAFE_SQL_PATTERN.search(value):
        raise ValueError("order_by contiene SQL no permitido.")
    return value


def _odata_filter_to_sql(filter_expression: object) -> tuple[str, list[object]]:
    if filter_expression is None or not str(filter_expression).strip():
        return "", []
    if not isinstance(filter_expression, str):
        raise ValueError("filter debe ser una expresión OData de texto.")

    token_pattern = re.compile(
        r"\s*(?:(?P<string>'(?:''|[^'])*')|"
        r"(?P<number>-?\d+(?:\.\d+)?)|"
        r"(?P<word>[A-Za-z_][A-Za-z0-9_]*)|"
        r"(?P<lparen>\()|(?P<rparen>\))|(?P<comma>,))"
    )
    tokens: list[tuple[str, str]] = []
    position = 0
    while position < len(filter_expression):
        match = token_pattern.match(filter_expression, position)
        if not match:
            if filter_expression[position:].strip() == "":
                break
            raise ValueError("filter contiene sintaxis OData no permitida.")
        position = match.end()
        kind = "word"
        value = ""
        for group in ("string", "number", "word", "lparen", "rparen", "comma"):
            if match.group(group) is not None:
                kind = group
                value = match.group(group)
                break
        tokens.append((kind, value))

    class ODataParser:
        def __init__(self, parsed_tokens: list[tuple[str, str]]) -> None:
            self.tokens = parsed_tokens
            self.index = 0
            self.values: list[object] = []

        def current(self) -> tuple[str, str] | None:
            if self.index >= len(self.tokens):
                return None
            return self.tokens[self.index]

        def consume(self, kind: str | None = None) -> tuple[str, str]:
            token = self.current()
            if token is None or (kind is not None and token[0] != kind):
                raise ValueError("filter contiene sintaxis OData no permitida.")
            self.index += 1
            return token

        def parse(self) -> str:
            result = self.parse_or()
            if self.current() is not None:
                raise ValueError("filter contiene sintaxis OData no permitida.")
            return result

        def parse_or(self) -> str:
            result = self.parse_and()
            while self._is_word("or"):
                self.consume("word")
                result = f"({result} OR {self.parse_and()})"
            return result

        def parse_and(self) -> str:
            result = self.parse_factor()
            while self._is_word("and"):
                self.consume("word")
                result = f"({result} AND {self.parse_factor()})"
            return result

        def parse_factor(self) -> str:
            if self.current() == ("lparen", "("):
                self.consume("lparen")
                result = self.parse_or()
                self.consume("rparen")
                return f"({result})"
            return self.parse_comparison()

        def parse_comparison(self) -> str:
            field = self.consume("word")[1]
            if self.current() == ("lparen", "("):
                return self.parse_function(field)
            operator = self.consume("word")[1].lower()
            operators = {
                "eq": "=", "ne": "<>", "gt": ">", "ge": ">=",
                "lt": "<", "le": "<=",
            }
            if operator not in operators:
                raise ValueError("filter usa un operador OData no permitido.")
            self.parse_literal()
            return f"[{field}] {operators[operator]} ?"

        def parse_function(self, name: str) -> str:
            if name.lower() not in {"contains", "startswith", "endswith"}:
                raise ValueError("filter usa una función OData no permitida.")
            self.consume("lparen")
            field = self.consume("word")[1]
            self.consume("comma")
            self.parse_literal()
            self.consume("rparen")
            pattern = {
                "contains": f"%{value}%",
                "startswith": f"{value}%",
                "endswith": f"%{value}",
            }[name.lower()]
            self.values[-1] = pattern
            return f"[{field}] LIKE ?"

        def parse_literal(self) -> object:
            kind, value = self.consume()
            if kind == "string":
                parsed: object = value[1:-1].replace("''", "'")
            elif kind == "number":
                parsed = float(value) if "." in value else int(value)
            elif kind == "word" and value.lower() in {"true", "false"}:
                parsed = value.lower() == "true"
            elif kind == "word" and value.lower() == "null":
                parsed = None
            else:
                raise ValueError("filter contiene un literal OData no permitido.")
            self.values.append(parsed)
            return parsed

        def _is_word(self, value: str) -> bool:
            token = self.current()
            return (
                token is not None
                and token[0] == "word"
                and token[1].lower() == value
            )

    parser = ODataParser(tokens)
    return parser.parse(), parser.values


def _build_query(
    parameters: object,
    object_name: object,
    filter_expression: object = None,
    order_by: object = None,
) -> tuple[str, str, list[object]]:
    configured_order_by = os.getenv("SQL_ORDER_BY", "")
    selected_order_by = (
        order_by if order_by is not None else configured_order_by
    )
    selected_order_by = _validate_order_by(selected_order_by)

    table = _resolve_object_name(object_name)
    columns = _get_requested_columns(parameters)
    filter_clause, filter_parameters = _odata_filter_to_sql(filter_expression)

    query = f"SELECT {columns} FROM {table}"
    count_query = f"SELECT COUNT_BIG(1) FROM {table}"
    if filter_clause:
        query += f" WHERE {filter_clause}"
        count_query += f" WHERE {filter_clause}"
    query += (
        f" ORDER BY {selected_order_by} "
        "OFFSET ? ROWS FETCH NEXT ? ROWS ONLY"
    )
    return query, count_query, filter_parameters


def _request_value(data: dict, req: func.HttpRequest, name: str) -> object:
    return data.get(name, req.params.get(name))


@app.route(
    route="swagger.json",
    methods=["GET"],
    auth_level=func.AuthLevel.ANONYMOUS,
)
def get_swagger_json(req: func.HttpRequest) -> func.HttpResponse:
    del req
    try:
        document = SWAGGER_FILE.read_text(encoding="utf-8")
    except OSError:
        logging.exception("No fue posible leer swagger.json")
        return func.HttpResponse(
            json.dumps({"error": "No fue posible cargar la documentación."}),
            mimetype="application/json",
            status_code=500,
        )
    return func.HttpResponse(
        document,
        mimetype="application/json",
        status_code=200,
    )


@app.route(route="func_SQL_Viewer", methods=["GET", "POST"])
def func_SQL_Viewer(req: func.HttpRequest) -> func.HttpResponse:
    try:
        data = _request_data(req)
        parameters = data.get("parameters", req.params.get("parameters", []))
        if isinstance(parameters, str):
            parameters = json.loads(parameters)
        if not isinstance(parameters, list):
            raise ValueError(
                "parameters debe ser una lista cuyos valores correspondan "
                "a los signos ? de SQL_FILTER_CLAUSE."
            )

        requested_limit = _request_value(data, req, "limit")
        if requested_limit is None:
            requested_limit = _request_value(data, req, "page_size")
        limit = _positive_int(requested_limit, "limit", 50)
        requested_offset = _request_value(data, req, "offset")
        page = _positive_int(
            _request_value(data, req, "page"), "page", 1
        )
        offset = _nonnegative_int(requested_offset, "offset", 0)
        if requested_offset is None:
            offset = (page - 1) * limit
        max_records = _positive_int(
            os.getenv("SQL_MAX_RECORDS", "1000"), "SQL_MAX_RECORDS", 1000
        )
        if offset >= max_records:
            raise ValueError(
                f"La página solicitada supera el máximo configurado de "
                f"{max_records} registros."
            )
        limit = min(limit, max_records - offset)
        if requested_offset is not None:
            page = (offset // limit) + 1

        include_total = _request_value(data, req, "include_total")
        if include_total is None:
            include_total = "false"
        include_total = str(include_total).lower() in {"1", "true", "yes"}
        query, count_query, filter_parameters = _build_query(
            parameters,
            object_name=_request_value(data, req, "object"),
            filter_expression=_request_value(data, req, "filter"),
            order_by=_request_value(data, req, "order_by"),
        )
        connection_string = _get_setting("SQL_CONNECTION_STRING")
        connection_timeout = _positive_int(
            os.getenv("SQL_CONNECTION_TIMEOUT", "5"),
            "SQL_CONNECTION_TIMEOUT",
            5,
        )
        query_timeout = _positive_int(
            os.getenv("SQL_QUERY_TIMEOUT", "30"), "SQL_QUERY_TIMEOUT", 30
        )

        pyodbc.pooling = True
        with pyodbc.connect(
            connection_string, timeout=connection_timeout
        ) as connection:
            connection.timeout = query_timeout
            cursor = connection.cursor()
            cursor.execute(query, *filter_parameters, offset, limit)
            columns = [column[0] for column in cursor.description]
            rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
            total = None
            if include_total:
                cursor.execute(count_query, *filter_parameters)
                total = int(cursor.fetchone()[0])

        pagination: dict[str, object] = {
            "page": page,
            "page_size": limit,
            "limit": limit,
            "offset": offset,
            "returned": len(rows),
            "has_more": (
                len(rows) == limit and offset + len(rows) < max_records
            ),
            "max_records": max_records,
        }
        pagination["next_page"] = (
            page + 1
            if pagination["has_more"]
            else None
        )
        response: dict[str, object] = {"data": rows, "pagination": pagination}
        if total is not None:
            pagination["total"] = min(total, max_records)
        return func.HttpResponse(
            json.dumps(response, default=_json_default),
            mimetype="application/json",
            status_code=200,
        )
    except ValueError as exc:
        return func.HttpResponse(
            json.dumps({"error": str(exc)}),
            mimetype="application/json",
            status_code=400,
        )
    except RuntimeError as exc:
        logging.error("Configuración inválida: %s", exc)
        return func.HttpResponse(
            json.dumps({"error": "Configuración inválida de la función."}),
            mimetype="application/json",
            status_code=500,
        )
    except pyodbc.Error:
        logging.exception("Error consultando Azure SQL")
        return func.HttpResponse(
            json.dumps({"error": "No fue posible consultar Azure SQL."}),
            mimetype="application/json",
            status_code=502,
        )
    except Exception:
        logging.exception("Error inesperado en la función SQL")
        return func.HttpResponse(
            json.dumps({"error": "Error interno de la función."}),
            mimetype="application/json",
            status_code=500,
        )
