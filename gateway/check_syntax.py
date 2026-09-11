#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读语法检查：AST 解析网关与连接器源码，不落盘、不执行。"""
import ast
import sys

FILES = ["gateway.py", "zcode_remote.py"]
ok = True
for fn in FILES:
    try:
        with open(fn, encoding="utf-8") as f:
            ast.parse(f.read())
        print("SYNTAX_OK", fn)
    except SyntaxError as e:
        ok = False
        print("SYNTAX_FAIL", fn, e.lineno, e.msg)
sys.exit(0 if ok else 1)
