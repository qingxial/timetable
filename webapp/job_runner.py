"""Private subprocess entry; the stdin payload is never written to disk."""
import json
from pathlib import Path
import sys
import os
import signal
import threading
import time


def main():
    parent = os.getppid()
    def guard_parent():
        while True:
            time.sleep(0.5)
            if os.getppid() != parent:
                os.killpg(os.getpgrp(), signal.SIGKILL)
    threading.Thread(target=guard_parent, daemon=True).start()
    payload = json.load(sys.stdin)
    import server
    server.PROJECT_ROOT = Path(payload['project_root'])
    server.DATA_ROOT = Path(payload['data_root'])
    sys.path.insert(0, str(server.PROJECT_ROOT))
    client = None
    if payload['client'] is not None:
        import llm_api
        client = llm_api.LLMClient(**payload['client'])
    request = payload['request']
    os.environ['TIMETABLE_PROGRESS_DIR'] = str(server.DATA_ROOT/'agent-runs'/request['run_id'])
    if request.get('intent') == 'scheduling_chat':
        from scheduling_chat import run_scheduling_chat
        run_scheduling_chat(request,server.PROJECT_ROOT,server.DATA_ROOT,client)
    elif request.get('intent') == 'room_review':
        from remaining_review import run_room_review
        run_room_review(request,server.PROJECT_ROOT,server.DATA_ROOT)
    elif request.get('intent') == 'one_off_review':
        from pending_review import run_one_off_review
        run_one_off_review(request,server.PROJECT_ROOT,server.DATA_ROOT)
    elif request.get('intent') == 'hour_review':
        from hour_review import run_hour_review
        run_hour_review(request,server.PROJECT_ROOT,server.DATA_ROOT,client)
    elif request.get('intent') == 'data_completion':
        from data_completion import run_completion
        run_completion(request,server.PROJECT_ROOT,server.DATA_ROOT,client)
    elif request.get('intent') == 'initial_schedule':
        from initial_schedule import run_initial
        run_initial(request, server.PROJECT_ROOT, server.DATA_ROOT)
    else:
        server._background_agent(request, client)


if __name__ == '__main__':
    main()
