from flask import Flask, jsonify, render_template, request

from .engine import (
    DEFAULT_OLLAMA_MODEL,
    get_formatter,
    infer_citation_style,
    normalize_citation_style,
    normalize_space,
    smart_parse_reference,
)

app = Flask(__name__, template_folder="../templates", static_folder="../static")


@app.route("/")
def index() -> str:
    return render_template("index.html")


@app.post("/api/format")
def format_reference():
    data = request.get_json(force=True) or {}
    source_type = data.get("source_type", "book")
    citation_style = normalize_citation_style(data.get("citation_style", "")) or infer_citation_style(
        data.get("authors", ""),
        data.get("title", ""),
        data.get("journal", ""),
        data.get("site_name", ""),
        data.get("url", ""),
    )
    formatter = get_formatter(source_type, citation_style)
    if not formatter:
        return jsonify({"error": "Неизвестный тип источника"}), 400
    data["citation_style"] = citation_style
    formatted = formatter(data)
    return jsonify({"formatted": formatted, "citation_style": citation_style})


@app.get("/api/parser-status")
def parser_status():
    return jsonify(
        {
            "llm_enabled": True,
            "provider": "ollama",
            "model": DEFAULT_OLLAMA_MODEL,
        }
    )


@app.post("/api/parse")
def parse_references():
    payload = request.get_json(force=True) or {}
    raw_text = payload.get("raw_text", "")
    requested_style = normalize_citation_style(payload.get("citation_style", ""))
    lines = [normalize_space(line) for line in raw_text.splitlines() if normalize_space(line)]
    results = []
    for line in lines:
        fields, parser_used, parser_error = smart_parse_reference(line)
        fields["citation_style"] = requested_style or normalize_citation_style(
            fields.get("citation_style", "")
        ) or infer_citation_style(line)
        formatter = get_formatter(fields["source_type"], fields["citation_style"])
        if not formatter:
            return jsonify({"error": "Неизвестный тип источника"}), 400
        formatted = formatter(fields)
        results.append(
            {
                "input": line,
                "detected_type": fields["source_type"],
                "fields": fields,
                "formatted": formatted,
                "parser_used": parser_used,
                "parser_error": parser_error,
            }
        )
    return jsonify({"results": results})
