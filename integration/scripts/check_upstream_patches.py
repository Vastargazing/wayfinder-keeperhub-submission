#!/usr/bin/env python3
"""Explicit unavailable result for the former combined upstream checker.

This snapshot supplies SDK checks separately. It cannot run the former complete
KeeperHub source validation; no success or equivalent coverage is implied.
"""
import json
import sys
if __name__ == '__main__':
    print(json.dumps({'status':'UNAVAILABLE','scope':'former combined upstream validation','exit':3,'replacement':'check_sdk_snapshot.py covers SDK only'}))
    sys.exit(3)
