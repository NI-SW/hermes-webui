"""全局文件状态：单一事实来源，无内部依赖。

文件注册表、inbox 注册表、inbox 锁、MEDIA 标记正则集中在此，
供 file_store 与 sse_proxy 共享，避免循环 import。
"""
from __future__ import annotations

import re
from threading import Lock

from models import FileRecord

# 普通 generated-files 注册表，token -> 文件记录
file_records: dict[str, FileRecord] = {}
# inbox report 注册表，token -> 文件记录
inbox_file_records: dict[str, FileRecord] = {}
# 保护 inbox_file_records 与磁盘元数据读写的互斥锁
inbox_lock = Lock()
# 匹配流式输出里的 MEDIA:/path/to/file 标记
MEDIA_PATTERN = re.compile(r"(?:^|\n)MEDIA:(?P<path>[^\r\n]+)")
