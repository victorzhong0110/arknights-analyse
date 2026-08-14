#!/bin/bash
# Mac 版私服服务器启动脚本
cd "$(dirname "$0")"
exec .venv/bin/python server/app.py
