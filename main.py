from __future__ import annotations

from multiprocessing import freeze_support

if __name__ == "__main__":
    freeze_support()
    
    import sys
    import signal
    import asyncio
    import logging
    import logging.handlers
    import argparse
    import warnings
    import traceback
    from functools import partial
    from typing import TYPE_CHECKING
    import truststore
    truststore.inject_into_ssl()

    from twitch import Twitch
    from settings import Settings
    from version import __version__
    from metrics import Metrics
    from exceptions import CaptchaRequired, LoginException
    from utils import lock_file
    from config_validator import startup_validation
    from constants import SELF_PATH, FILE_FORMATTER, LOG_PATH, LOCK_PATH, WORKING_DIR

    if TYPE_CHECKING:
        from _typeshed import SupportsWrite

    warnings.simplefilter("default", ResourceWarning)

    if sys.version_info < (3, 10):
        raise RuntimeError("Python 3.10 or higher is required")

    class ParsedArgs(argparse.Namespace):
        _verbose: int
        _debug_ws: bool
        _debug_gql: bool
        log: bool
        dump: bool

        @property
        def debug_ws(self) -> int:
            if self._debug_ws:
                return logging.DEBUG
            elif self._verbose >= 4:
                return logging.INFO
            return logging.NOTSET

        @property
        def debug_gql(self) -> int:
            if self._debug_gql:
                return logging.DEBUG
            elif self._verbose >= 4:
                return logging.INFO
            return logging.NOTSET

    # Handle input parameters
    parser = argparse.ArgumentParser(
        SELF_PATH.name,
        description="A program that allows you to mine timed drops on Twitch.",
    )
    parser.add_argument("--version", action="version", version=f"v{__version__}")
    parser.add_argument("-v", dest="_verbose", action="count", default=0)
    parser.add_argument("--log", action="store_true")
    parser.add_argument("--dump", action="store_true")
    parser.add_argument(
        "--debug-ws", dest="_debug_ws", action="store_true", help=argparse.SUPPRESS
    )
    parser.add_argument(
        "--debug-gql", dest="_debug_gql", action="store_true", help=argparse.SUPPRESS
    )
    args = parser.parse_args(namespace=ParsedArgs())
    
    try:
        settings = Settings(args)
    except Exception as e:
        print(f"Settings error: {traceback.format_exc()}")
        raise e

    def setup_logging(settings: Settings, args: ParsedArgs) -> logging.Logger:
        """Configure logging with multiple handlers for different purposes"""
        LOG_LEVEL_MAP = {
            "DEBUG": logging.DEBUG,
            "INFO": logging.INFO,
            "WARNING": logging.WARNING,
            "ERROR": logging.ERROR,
        }

        log_level_setting = getattr(settings, "logging_level", "INFO")
        if not isinstance(log_level_setting, str):
            log_level_setting = "INFO"
        log_level_str = log_level_setting.upper()
        log_level = LOG_LEVEL_MAP.get(log_level_str, logging.INFO)

        logger = logging.getLogger("TwitchDrops")
        logger.setLevel(logging.DEBUG)  # Capture all, filter at handler level

        # Console handler - main output
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(log_level)
        console_handler.setFormatter(FILE_FORMATTER)
        logger.addHandler(console_handler)

        if settings.log:
            # Ensure log directory exists
            log_dir = LOG_PATH.parent
            log_dir.mkdir(parents=True, exist_ok=True)
            
            # Main log file - everything
            main_handler = logging.handlers.RotatingFileHandler(
                log_dir / "main.log",
                maxBytes=10*1024*1024,  # 10MB
                backupCount=3,
                encoding='utf-8'
            )
            main_handler.setLevel(log_level)
            main_handler.setFormatter(FILE_FORMATTER)
            logger.addHandler(main_handler)
            
            # Error log file - errors only
            error_handler = logging.handlers.RotatingFileHandler(
                log_dir / "errors.log",
                maxBytes=5*1024*1024,  # 5MB
                backupCount=2,
                encoding='utf-8'
            )
            error_handler.setLevel(logging.ERROR)
            error_handler.setFormatter(FILE_FORMATTER)
            logger.addHandler(error_handler)
            
            # Debug log file - debug level (if enabled)
            if log_level == logging.DEBUG:
                debug_handler = logging.handlers.RotatingFileHandler(
                    log_dir / "debug.log",
                    maxBytes=20*1024*1024,  # 20MB
                    backupCount=2,
                    encoding='utf-8'
                )
                debug_handler.setLevel(logging.DEBUG)
                debug_handler.setFormatter(FILE_FORMATTER)
                logger.addHandler(debug_handler)

        # Set levels for subloggers
        gql_level = logging.INFO if log_level == logging.DEBUG else log_level

        # WebSocket sub-logger: clamp to WARNING minimum unless --debug-ws is explicitly set.
        # This suppresses constant reconnect/closed-by-server noise at INFO level,
        # while still surfacing PONG timeouts, errors, and blacklists.
        if args._debug_ws:
            ws_level = logging.DEBUG
        else:
            ws_level = max(log_level, logging.WARNING)

        if args._debug_gql:
            gql_level = logging.DEBUG

        logging.getLogger("TwitchDrops.gql").setLevel(gql_level)
        logging.getLogger("TwitchDrops.websocket").setLevel(ws_level)
        
        return logger

    async def main():
        logger = setup_logging(settings, args)
        
        # Print startup banner
        logger.info("=" * 60)
        logger.info(f"Twitch Drops Miner v{__version__}")
        logger.info("=" * 60)
        
        # Run configuration validation
        if not startup_validation(settings):
            logger.error("Configuration validation failed. Please fix issues and restart.")
            sys.exit(2)
        
        # Initialize metrics
        metrics = Metrics(WORKING_DIR / "metrics.json")
        
        exit_status = 0
        client = Twitch(settings)
        
        # Attach metrics to client
        client.metrics = metrics
        
        loop = asyncio.get_running_loop()
        def signal_handler(sig):
            sig_name = signal.Signals(sig).name if hasattr(signal, 'Signals') else str(sig)
            logger.info(f"Received signal {sig_name}, initiating graceful shutdown...")
            client.close()

        # Register signals for graceful Docker shutdown
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, partial(signal_handler, sig))
            except NotImplementedError:
                # Windows doesn't support add_signal_handler
                signal.signal(sig, lambda s, f: signal_handler(s))
        # -------------------------------------------
        
        try:
            logger.info("Starting Twitch Drops Miner...")
            await client.run()
        except CaptchaRequired:
            exit_status = 1
            client.prevent_close()
            logger.error(
                "CAPTCHA required\n"
                "Your login attempt was denied by CAPTCHA.\n"
                "Please wait 12+ hours before trying again."
            )
        except LoginException as e:
            exit_status = 1
            client.prevent_close()
            logger.error(
                f"Login failed\n"
                f"Reason: {e}\n\n"
                f"Troubleshooting:\n"
                f"1. Ensure cookies.jar exists and is readable\n"
                f"2. Verify cookies.jar is from a recent Twitch login\n"
                f"3. Check that the cookie file isn't corrupted\n"
                f"4. Try generating a fresh cookies.jar"
            )
        except KeyboardInterrupt:
            logger.info("Keyboard interrupt received")
        except Exception as exc:
            exit_status = 1
            client.prevent_close()
            logger.error(f"Fatal error:\n{traceback.format_exc()}")
            metrics.record_error("fatal_exception")
        finally:
            logger.info("Shutting down gracefully...")
            
            # Print metrics summary before shutdown
            if metrics:
                logger.info("\n" + metrics.get_summary())
            
            await client.shutdown()
        
        if not client.close_requested:
            logger.error("Application terminated unexpectedly")

        client.save(force=True)
        
        logger.info("=" * 60)
        logger.info(f"Twitch Drops Miner stopped. Exit code: {exit_status}")
        logger.info("=" * 60)
        
        sys.exit(exit_status)

    try:
        # Lock file check
        success, file = lock_file(LOCK_PATH)
        if not success:
            print("Another instance is already running!")
            print(f"Lock file exists at: {LOCK_PATH}")
            print("If you're sure no other instance is running, delete the lock file.")
            sys.exit(3)
        
        print(f"Lock file acquired: {LOCK_PATH}")
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nInterrupted by user")
        sys.exit(130)
    except Exception as e:
        print(f"Startup error: {e}")
        traceback.print_exc()
        sys.exit(1)
    finally:
        if 'file' in locals():
            try:
                file.close()
                LOCK_PATH.unlink(missing_ok=True)
            except Exception:
                pass
