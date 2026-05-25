"""Ex5 tools. Four tools the agent uses to research an Edinburgh booking.

Each tool:
  1. Reads its fixture from sample_data/ (DO NOT modify the fixtures).
  2. Logs its arguments and output into _TOOL_CALL_LOG (see integrity.py).
  3. Returns a ToolResult with success=True/False, output=dict, summary=str.

The grader checks for:
  * Correct parallel_safe flags (reads True, generate_flyer False).
  * Every tool's results appear in _TOOL_CALL_LOG.
  * Tools fail gracefully on missing fixtures or bad inputs (ToolError,
    not RuntimeError).
"""

from __future__ import annotations

import json
from pathlib import Path

from sovereign_agent.errors import ToolError
from sovereign_agent.session.directory import Session
from sovereign_agent.tools.registry import ToolRegistry, ToolResult, _RegisteredTool

from starter.edinburgh_research.integrity import _TOOL_CALL_LOG, record_tool_call

_SAMPLE_DATA = Path(__file__).parent / "sample_data"


# ---------------------------------------------------------------------------
# TODO 1 — venue_search
# ---------------------------------------------------------------------------
def venue_search(near: str, party_size: int, budget_max_gbp: int = 1000) -> ToolResult:
    """Search for Edinburgh venues near <near> that can seat the party.

    Reads sample_data/venues.json. Filters by:
      * open_now == True
      * area contains <near> (case-insensitive substring match)
      * seats_available_evening >= party_size
      * hire_fee_gbp + min_spend_gbp <= budget_max_gbp

    Returns a ToolResult with:
      output: {"near": ..., "party_size": ..., "results": [<venue dicts>], "count": int}
      summary: "venue_search(<near>, party=<N>): <count> result(s)"

    MUST call record_tool_call(...) before returning so the integrity
    check can see what data was produced.
    """
    # Spiral detection — after 3+ calls, force a stop
    search_count = sum(1 for r in _TOOL_CALL_LOG if r.tool_name == "venue_search")
    if search_count >= 3:
        err = ToolError(code="SA_TOOL_SPIRAL", message="too_many_searches")
        output = {"error": str(err)}
        summary = "STOP calling venue_search; use the results you already have."

        record_tool_call(
            "venue_search",
            {"near": near, "party_size": party_size, "budget_max_gbp": budget_max_gbp},
            output,
        )
        return ToolResult(success=False, output=output, summary=summary, error=err)

    venues_file = _SAMPLE_DATA / "venues.json"

    if not venues_file.exists():
        raise ToolError(code="SA_TOOL_INVALID_INPUT", message="Venues data file doesn't exist")

    with open(venues_file, encoding="utf-8") as f:
        venues_data = json.load(f)

    near_norm = near.lower().strip()
    near_tokens = set(near_norm.split())

    results = []
    for v in venues_data:
        if not v.get("open_now"):
            continue

        area_norm = v.get("area", "").lower().strip()
        area_tokens = set(area_norm.split())
        matches_area = (
            near_norm in area_norm or area_norm in near_norm or bool(near_tokens & area_tokens)
        )
        if not matches_area:
            continue

        if v.get("seats_available_evening", 0) < party_size:
            continue

        total_cost = v.get("hire_fee_gbp", 0) + v.get("min_spend_gbp", 0)
        if total_cost > budget_max_gbp:
            continue

        results.append(v)

    output = {"near": near, "party_size": party_size, "results": results, "count": len(results)}
    summary = f"venue_search({near}, party={party_size}): {len(results)} result(s)"

    record_tool_call(
        "venue_search",
        {"near": near, "party_size": party_size, "budget_max_gbp": budget_max_gbp},
        output,
    )
    return ToolResult(success=True, output=output, summary=summary)


# ---------------------------------------------------------------------------
# TODO 2 — get_weather
# ---------------------------------------------------------------------------
def get_weather(city: str, date: str) -> ToolResult:
    """Look up the scripted weather for <city> on <date> (YYYY-MM-DD).

    Reads sample_data/weather.json. Returns:
      output: {"city": str, "date": str, "condition": str, "temperature_c": int, ...}
      summary: "get_weather(<city>, <date>): <condition>, <temp>C"

    If the city or date is not in the fixture, return success=False with
    a clear ToolError (SA_TOOL_INVALID_INPUT). Do NOT raise.

    MUST call record_tool_call(...) before returning.
    """
    weather_file = _SAMPLE_DATA / "weather.json"

    if not weather_file.exists():
        raise ToolError(code="SA_TOOL_INVALID_INPUT", message="Weather data file doesn't exist")

    with open(weather_file, encoding="utf-8") as f:
        weather_data = json.load(f)

    city_data = weather_data.get(city.lower())

    if not city_data or date not in city_data:
        err = ToolError(
            code="SA_TOOL_INVALID_INPUT",
            message="Chosen city not found, or no weather data for the chosen city",
        )
        output = {"error": str(err)}
        summary = f"get_weather({city}, {date}): Weather data not found"

        record_tool_call("get_weather", {"city": city, "date": date}, output)
        return ToolResult(success=False, output=output, summary=summary, error=err)

    day_weather = city_data[date]
    output = {
        "city": city,
        "date": date,
        "condition": day_weather["condition"],
        "temperature_c": day_weather["temperature_c"],
    }
    summary = f"get_weather({city}, {date}): {output['condition']}, {output['temperature_c']}C"

    record_tool_call("get_weather", {"city": city, "date": date}, output)
    return ToolResult(success=True, output=output, summary=summary)


# ---------------------------------------------------------------------------
# TODO 3 — calculate_cost
# ---------------------------------------------------------------------------
def calculate_cost(
    venue_id: str,
    party_size: int,
    duration_hours: int,
    catering_tier: str = "bar_snacks",
) -> ToolResult:
    """Compute the total cost for a booking.

    Formula:
      base_per_head = base_rates_gbp_per_head[catering_tier]
      venue_mult    = venue_modifiers[venue_id]
      subtotal      = base_per_head * venue_mult * party_size * max(1, duration_hours)
      service       = subtotal * service_charge_percent / 100
      total         = subtotal + service + <venue's hire_fee_gbp + min_spend_gbp>
      deposit_rule  = per deposit_policy thresholds

    Returns:
      output: {
        "venue_id": str,
        "party_size": int,
        "duration_hours": int,
        "catering_tier": str,
        "subtotal_gbp": int,
        "service_gbp": int,
        "total_gbp": int,
        "deposit_required_gbp": int,
      }
      summary: "calculate_cost(<venue>, <party>): total £<N>, deposit £<M>"

    MUST call record_tool_call(...) before returning.
    """
    catering_file = _SAMPLE_DATA / "catering.json"
    venues_file = _SAMPLE_DATA / "venues.json"

    if not catering_file.exists() or not venues_file.exists():
        raise ToolError(
            code="SA_TOOL_INVALID_INPUT",
            message="Either catering or venues data file doesn't exist, or both don't exist",
        )

    with open(catering_file, encoding="utf-8") as f:
        catering_data = json.load(f)

    with open(venues_file, encoding="utf-8") as f:
        venues_data = json.load(f)

    venue = next((v for v in venues_data if v["id"] == venue_id), None)
    if not venue:
        err = ToolError(code="SA_TOOL_INVALID_INPUT", message="Venue ID not found")
        output = {"error": str(err)}
        summary = f"calculate_cost({venue_id}, {party_size}): Venue not found"

        record_tool_call("calculate_cost", {"venue_id": venue_id, "party_size": party_size}, output)
        return ToolResult(success=False, output=output, summary=summary, error=err)

    base_per_head = catering_data["base_rates_gbp_per_head"].get(catering_tier, 0)
    venue_mult = catering_data["venue_modifiers"].get(venue_id, 1.0)

    subtotal = base_per_head * venue_mult * party_size * max(1, duration_hours)
    service = subtotal * catering_data["service_charge_percent"] / 100

    venue_fees = venue.get("hire_fee_gbp", 0) + venue.get("min_spend_gbp", 0)
    total = subtotal + service + venue_fees

    # Calculate deposit
    if total < 300:
        deposit = 0
    elif total <= 1000:
        deposit = total * 0.20
    else:
        deposit = total * 0.30

    output = {
        "venue_id": venue_id,
        "party_size": party_size,
        "duration_hours": duration_hours,
        "catering_tier": catering_tier,
        "subtotal_gbp": int(subtotal),
        "service_gbp": int(service),
        "total_gbp": int(total),
        "deposit_required_gbp": int(deposit),
    }
    summary = f"calculate_cost({venue_id}, {party_size}): total £{output['total_gbp']}, deposit £{output['deposit_required_gbp']}"

    record_tool_call("calculate_cost", {"venue_id": venue_id, "party_size": party_size}, output)
    return ToolResult(success=True, output=output, summary=summary)


# ---------------------------------------------------------------------------
# TODO 4 — generate_flyer
# ---------------------------------------------------------------------------
def generate_flyer(session: Session, event_details: dict) -> ToolResult:
    """Produce an HTML flyer and write it to workspace/flyer.html.

    event_details is expected to contain at least:
      venue_name, venue_address, date, time, party_size, condition,
      temperature_c, total_gbp, deposit_required_gbp

    Write a self-contained HTML flyer (inline CSS, no external assets). Tag every key fact with data-testid="<n>" so the integrity check can parse it.

    Write a formatted HTML flyer with an H1 title, the event
    facts, a weather summary, and the cost breakdown.

    Returns:
      output: {"path": "workspace/flyer.html", "bytes_written": int}
      summary: "generate_flyer: wrote <path> (<N> chars)"

    MUST call record_tool_call(...) before returning — the integrity
    check compares the flyer's contents against earlier tool outputs.

    IMPORTANT: this tool MUST be registered with parallel_safe=False
    because it writes a file.
    """
    required_fields = [
        "venue_name",
        "venue_address",
        "date",
        "time",
        "party_size",
        "condition",
        "temperature_c",
        "total_gbp",
        "deposit_required_gbp",
    ]
    missing = [k for k in required_fields if event_details.get(k) in (None, "", "None")]
    if missing:
        err = ToolError(
            code="SA_TOOL_INVALID_INPUT",
            message=(
                f"generate_flyer requires these fields populated: {missing}. "
                f"Pull venue_name/venue_address from venue_search results, "
                f"condition/temperature_c from get_weather, and "
                f"total_gbp/deposit_required_gbp from calculate_cost. "
                f"Look at the PRIOR CONTEXT block from earlier tool calls."
            ),
        )
        output = {"error": str(err), "missing_fields": missing}
        summary = f"generate_flyer rejected: missing fields {missing}"
        record_tool_call("generate_flyer", {"event_details": event_details}, output)
        return ToolResult(success=False, output=output, summary=summary, error=err)

    html_content = f"""
    <html>
    <body>
        <h1>Event Flyer</h1>
        <ul>
            <li>Venue: <span data-testid="venue_name">{event_details.get("venue_name")}</span></li>
            <li>Address: <span data-testid="venue_address">{event_details.get("venue_address")}</span></li>
            <li>Date: <span data-testid="date">{event_details.get("date")}</span></li>
            <li>Time: <span data-testid="time">{event_details.get("time")}</span></li>
            <li>Party Size: <span data-testid="party_size">{event_details.get("party_size")}</span></li>
            <li>Weather: <span data-testid="condition">{event_details.get("condition")}</span></li>
            <li>Temperature: <span data-testid="temperature_c">{event_details.get("temperature_c")}</span>°C</li>
            <li>Total Cost: £<span data-testid="total_gbp">{event_details.get("total_gbp")}</span></li>
            <li>Deposit: £<span data-testid="deposit_required_gbp">{event_details.get("deposit_required_gbp")}</span></li>
        </ul>
    </body>
    </html>
    """

    # Save the flyer
    flyer_path = session.workspace_dir / "flyer.html"
    flyer_path.write_text(html_content, encoding="utf-8")

    output = {"path": "workspace/flyer.html", "bytes_written": len(html_content.encode("utf-8"))}
    summary = f"generate_flyer: wrote workspace/flyer.html ({len(html_content)} chars)"

    record_tool_call("generate_flyer", {"event_details": event_details}, output)
    return ToolResult(success=True, output=output, summary=summary)


# ---------------------------------------------------------------------------
# Registry builder — DO NOT MODIFY the name, signature, or registration calls.
# The grader imports and calls this to pick up your tools.
# ---------------------------------------------------------------------------
def build_tool_registry(session: Session) -> ToolRegistry:
    """Build a session-scoped tool registry with all four Ex5 tools plus
    the sovereign-agent builtins (read_file, write_file, list_files,
    handoff_to_structured, complete_task).

    DO NOT change the tool names — the tests and grader call them by name.
    """
    from sovereign_agent.tools.builtin import make_builtin_registry

    reg = make_builtin_registry(session)

    # venue_search
    reg.register(
        _RegisteredTool(
            name="venue_search",
            description="Search Edinburgh venues by area, party size, and max budget.",
            fn=venue_search,
            parameters_schema={
                "type": "object",
                "properties": {
                    "near": {"type": "string"},
                    "party_size": {"type": "integer"},
                    "budget_max_gbp": {"type": "integer", "default": 1000},
                },
                "required": ["near", "party_size"],
            },
            returns_schema={"type": "object"},
            is_async=False,
            parallel_safe=True,  # read-only
            examples=[
                {
                    "input": {"near": "Haymarket", "party_size": 6, "budget_max_gbp": 800},
                    "output": {"count": 1, "results": [{"id": "haymarket_tap"}]},
                }
            ],
        )
    )

    # get_weather
    reg.register(
        _RegisteredTool(
            name="get_weather",
            description="Get scripted weather for a city on a YYYY-MM-DD date.",
            fn=get_weather,
            parameters_schema={
                "type": "object",
                "properties": {
                    "city": {"type": "string"},
                    "date": {"type": "string"},
                },
                "required": ["city", "date"],
            },
            returns_schema={"type": "object"},
            is_async=False,
            parallel_safe=True,  # read-only
            examples=[
                {
                    "input": {"city": "Edinburgh", "date": "2026-04-25"},
                    "output": {"condition": "cloudy", "temperature_c": 12},
                }
            ],
        )
    )

    # calculate_cost
    reg.register(
        _RegisteredTool(
            name="calculate_cost",
            description="Compute total cost and deposit for a booking.",
            fn=calculate_cost,
            parameters_schema={
                "type": "object",
                "properties": {
                    "venue_id": {"type": "string"},
                    "party_size": {"type": "integer"},
                    "duration_hours": {"type": "integer"},
                    "catering_tier": {
                        "type": "string",
                        "enum": ["drinks_only", "bar_snacks", "sit_down_meal", "three_course_meal"],
                        "default": "bar_snacks",
                    },
                },
                "required": ["venue_id", "party_size", "duration_hours"],
            },
            returns_schema={"type": "object"},
            is_async=False,
            parallel_safe=True,  # pure compute, no shared state
            examples=[
                {
                    "input": {
                        "venue_id": "haymarket_tap",
                        "party_size": 6,
                        "duration_hours": 3,
                    },
                    "output": {"total_gbp": 540, "deposit_required_gbp": 0},
                }
            ],
        )
    )

    # generate_flyer — parallel_safe=False because it writes a file
    def _flyer_adapter(event_details: dict) -> ToolResult:
        return generate_flyer(session, event_details)

    reg.register(
        _RegisteredTool(
            name="generate_flyer",
            description=(
                "Write an HTML event flyer to workspace/flyer.html. "
                "ALL fields of event_details are required — populate them from "
                "earlier tool outputs: venue_name/venue_address from venue_search "
                "results, condition/temperature_c from get_weather, "
                "total_gbp/deposit_required_gbp from calculate_cost."
            ),
            fn=_flyer_adapter,
            parameters_schema={
                "type": "object",
                "properties": {
                    "event_details": {
                        "type": "object",
                        "properties": {
                            "venue_name": {
                                "type": "string",
                                "description": "Pub name, e.g. 'Haymarket Tap' (from venue_search results[].name).",
                            },
                            "venue_address": {
                                "type": "string",
                                "description": "Full address (from venue_search results[].address).",
                            },
                            "date": {
                                "type": "string",
                                "description": "Event date in YYYY-MM-DD format.",
                            },
                            "time": {
                                "type": "string",
                                "description": "Event start time in HH:MM (24h).",
                            },
                            "party_size": {
                                "type": "integer",
                                "description": "Number of guests.",
                            },
                            "condition": {
                                "type": "string",
                                "description": "Weather condition (from get_weather output, e.g. 'cloudy').",
                            },
                            "temperature_c": {
                                "type": "number",
                                "description": "Temperature in Celsius (from get_weather output).",
                            },
                            "total_gbp": {
                                "type": "number",
                                "description": "Total cost in GBP (from calculate_cost output total_gbp).",
                            },
                            "deposit_required_gbp": {
                                "type": "number",
                                "description": "Deposit in GBP (from calculate_cost output deposit_required_gbp).",
                            },
                        },
                        "required": [
                            "venue_name",
                            "venue_address",
                            "date",
                            "time",
                            "party_size",
                            "condition",
                            "temperature_c",
                            "total_gbp",
                            "deposit_required_gbp",
                        ],
                    }
                },
                "required": ["event_details"],
            },
            returns_schema={"type": "object"},
            is_async=False,
            parallel_safe=False,  # writes a file — MUST be False
            examples=[
                {
                    "input": {
                        "event_details": {
                            "venue_name": "Haymarket Tap",
                            "venue_address": "12 Dalry Rd, Edinburgh EH11 2BG",
                            "date": "2026-04-25",
                            "time": "19:30",
                            "party_size": 6,
                            "condition": "cloudy",
                            "temperature_c": 12,
                            "total_gbp": 556,
                            "deposit_required_gbp": 111,
                        }
                    },
                    "output": {"path": "workspace/flyer.html", "bytes_written": 790},
                }
            ],
        )
    )

    return reg


__all__ = [
    "build_tool_registry",
    "venue_search",
    "get_weather",
    "calculate_cost",
    "generate_flyer",
]
