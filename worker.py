"""Entry point for the sazed-worker service (`python3 /opt/sazed-worker/worker.py`).

The code is in the `sazed_worker` package next to this file; see its docstring for the layout.
"""

import logging

from sazed_worker.main import main

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    main()
