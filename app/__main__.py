"""通过 python -m app 启动 Worker，默认监听本机 13371 端口。"""

import argparse
import os
from pathlib import Path

import uvicorn
from dotenv import load_dotenv


def main(argv: list[str] | None = None) -> None:
    """加载选定配置后启动服务；系统环境变量优先于文件配置。"""
    parser = argparse.ArgumentParser(description="启动 Python Worker 服务")
    parser.add_argument("--env-file", type=Path, help="指定配置文件，例如 production.env；默认读取 .env")
    args = parser.parse_args(argv)
    if args.env_file is not None:
        if not args.env_file.is_file():
            parser.error(f"配置文件不存在或不是文件: {args.env_file}")
        load_dotenv(args.env_file)
    else:
        load_dotenv()
    uvicorn.run(
        "app.main:app",
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "13371")),
        workers=1,
    )


if __name__ == "__main__":
    main()
