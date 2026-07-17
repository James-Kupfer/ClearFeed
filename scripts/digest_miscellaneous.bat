@echo off
call "%~dp0..\clearfeed.bat" dispatch profiles\digest_miscellaneous.yaml
timeout /t 250
