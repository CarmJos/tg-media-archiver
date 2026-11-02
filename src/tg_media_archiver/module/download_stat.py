"""Current transfer samples consumed by the manual progress panel."""

import time

_downloads = {}


def get_download_result():
    return _downloads


async def update_download_status(
    down_byte, total_size, message_id, file_name, start_time, node, client=None
):
    now = time.time()
    records = _downloads.setdefault(node.chat_id, {})
    previous = records.get(message_id, {})
    same_task = previous.get("task_id") == node.task_id
    previous_bytes = previous.get("down_byte", 0) if same_task else 0
    previous_time = previous.get("end_time", start_time) if same_task else start_time
    elapsed = max(now - previous_time, 0.001)
    records[message_id] = {
        "task_id": node.task_id,
        "file_name": file_name,
        "down_byte": down_byte,
        "total_size": total_size,
        "end_time": now,
        "download_speed": max(0, (down_byte - previous_bytes) / elapsed),
    }
