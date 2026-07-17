@echo off
call "%~dp0..\clearfeed.bat" dispatch profiles\digest_personal.yaml
timeout /t 250
