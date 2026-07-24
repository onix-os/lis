"""ANSI Color & Formatting Utilities for LIS E2E Test Suite."""

RESET = "\033[0m"
BOLD = "\033[1m"
GREEN = "\033[38;2;46;204;113m"
RED = "\033[38;2;231;76;60m"
YELLOW = "\033[38;2;241;196;15m"
CYAN = "\033[38;2;52;152;219m"
MAGENTA = "\033[38;2;155;89;182m"
WHITE = "\033[38;2;236;240;241m"
GRAY = "\033[38;2;127;140;141m"

TICK = f"{GREEN}✓{RESET}"
CROSS = f"{RED}✗{RESET}"
WARN_ICON = f"{YELLOW}⚠{RESET}"


def print_stage_header(stage_num: int, title: str):
    print(f"\n{MAGENTA}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{RESET}")
    print(f"{BOLD}{CYAN}STAGE {stage_num}: {title.upper()}{RESET}")
    print(f"{MAGENTA}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{RESET}")


def print_check_item(name: str, status: bool, detail: str = ""):
    icon = TICK if status else CROSS
    color = GREEN if status else RED
    detail_str = f" {GRAY}({detail}){RESET}" if detail else ""
    print(f"  [{icon}] {BOLD}{name}{RESET}:{color} {'PASSED' if status else 'FAILED'}{RESET}{detail_str}")
