# Timetable Scheduling Agent

A university timetable scheduler with a greedy initial scheduling engine, local repair and rescheduling, and an LLM agent that selects scheduling and diagnostic tools. Candidate schedules are checked against the active rules before they are saved.

## Run locally

Use Python 3.11 or 3.12 from the repository root.

```sh
python -m pip install -r requirements.txt
python webapp/server.py
```

Open http://127.0.0.1:8765 and supply your own scheduling workbooks in the web interface. On a first run, provide the course, teacher, room, and class workbooks. Rule-based scheduling is available without a model provider. To use the LLM agent, configure an API key in the local interface or set `DASHSCOPE_API_KEY`. `DASHSCOPE_BASE_URL` and `DASHSCOPE_MODEL` are optional for the built-in client.

## Source layout

- `test_for_school.py` is the original production scheduling engine used by the web app.
- `reschedule_by_adjusting.py` implements the original rescheduling engine.
- `scripts/timetable_agent_loop.py` and `scripts/timetable_agent_tools.py` contain the agent loop and tool interface.
- `webapp/` contains the local interface, server, review workflows, and quality report template.

The application keeps generated files, local configuration, and timetable versions under `webapp/data/`. Supply institutional data separately.
