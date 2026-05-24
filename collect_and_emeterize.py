#!/usr/bin/env python3
# coding=utf-8
"""Entry point for the SMA data aggregator and virtual emeter."""

import logging

from sma_emeter.scheduler import main

if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        logging.info("Program terminating...")
