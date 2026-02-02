"""
Entry point for running git-p4son as a module: python -m git_p4son
"""

import sys
from .cli import main

if __name__ == '__main__':
    exit_code = main()
    sys.exit(exit_code)
