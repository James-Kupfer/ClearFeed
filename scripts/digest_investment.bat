@echo off
call "%~dp0..\clearfeed.bat" dispatch profiles\investment_digest.yaml
timeout /t 250
