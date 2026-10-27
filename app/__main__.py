"""通过 python -m app 启动 Worker，默认监听本机 13371 端口。"""

import os

import uvicorn
from dotenv import load_dotenv


def main() -> None:
    """先加载本地 .env，再导入应用；系统环境变量优先于文件配置。"""
    load_dotenv()
    uvicorn.run(
        "app.main:app",
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "13371")),
        workers=1,
    )


if __name__ == "__main__":
    main()
