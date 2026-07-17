@echo off
call "%~dp0..\clearfeed.bat" dispatch profiles\digest_professional.yaml
timeout /t 250
