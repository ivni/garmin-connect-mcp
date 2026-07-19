"""Command-line entry point for Garmin Connect MCP."""

import sys

AUTH_USAGE = (
    "Usage: garmin-connect-mcp auth "
    "[doctor|migrate [--yes] [--include-local-env] [--allow-custom-legacy] [--purge]]"
)


def main() -> None:
    """Run the MCP server or a supported subcommand."""
    args = sys.argv[1:]

    if not args:
        from .server import main as server_main

        server_main()
        return

    if args and args[0] == "auth":
        from .scripts.setup_auth import main as auth_main

        auth_args = args[1:]
        if not auth_args:
            raise SystemExit(auth_main())
        if auth_args[0] in {"-h", "--help", "help"}:
            print(AUTH_USAGE)
            return
        if auth_args == ["doctor"]:
            raise SystemExit(auth_main("doctor"))
        if auth_args and auth_args[0] == "migrate":
            flags = set(auth_args[1:])
            allowed_flags = {
                "--yes",
                "--include-local-env",
                "--allow-custom-legacy",
                "--purge",
            }
            if len(flags) == len(auth_args[1:]) and flags <= allowed_flags:
                raise SystemExit(
                    auth_main(
                        "migrate",
                        assume_yes="--yes" in flags,
                        include_local_env="--include-local-env" in flags,
                        allow_custom_legacy="--allow-custom-legacy" in flags,
                        purge_quarantine="--purge" in flags,
                    )
                )
        print(f"Unknown auth command: {' '.join(auth_args)}", file=sys.stderr)
        print(AUTH_USAGE, file=sys.stderr)
        sys.exit(2)

    if args[0] in {"-h", "--help", "help"}:
        print("Usage: garmin-connect-mcp [auth ...]")
        return

    print(f"Unknown command: {args[0]}", file=sys.stderr)
    print("Usage: garmin-connect-mcp [auth ...]", file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    main()
