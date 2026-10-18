"""入口：默认启动 GUI；--headless 仅启动 MCP 服务（用于测试/无界面环境）。"""
import logging
import sys


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    if "--headless" in sys.argv:
        import time

        from app_config import ConfigManager
        from mcp_service import McpService

        cm = ConfigManager()
        service = McpService(cm)
        ok, msg = service.start()
        print(msg, flush=True)
        if not ok:
            sys.exit(1)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            service.stop()
    else:
        if "--tk" in sys.argv:
            from gui import run_app
        else:
            from webui import run_app

        run_app()


if __name__ == "__main__":
    main()
