import os
import tempfile

# 在导入应用前指定临时 SQLite 文件，避免污染开发库
if "DB_PATH" not in os.environ:
    _fd, _path = tempfile.mkstemp(suffix=".db")
    os.close(_fd)
    os.remove(_path)
    os.environ["DB_PATH"] = _path
