@echo off
call "%~dp0..\clearfeed.bat" dispatch profiles\digest_science_and_medicine.yaml
timeout /t 250
