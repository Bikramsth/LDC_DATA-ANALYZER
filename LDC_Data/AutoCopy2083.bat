@echo off
:: Set paths
set "SOURCE_DIR=D:\DATA\0 Data_Collection 75\Daily_Log_Sheets\Log Sheet 2083"
set "DEST_DIR=C:\Users\CRLDC\Documents\GitHub\LDC_DATA-ANALYZER\LDC_Data\Log Sheet 2083"

:: Set log file location (saving to your Desktop)
set "LOG_FILE=C:\Users\CRLDC\Desktop\CopyLog.txt"

:: Extract folder names
for %%F in ("%SOURCE_DIR%") do set "SOURCE_NAME=%%~nxF"
for %%F in ("%DEST_DIR%") do set "DEST_NAME=%%~nxF"

:: Check names and execute Robocopy
if /I "%SOURCE_NAME%"=="%DEST_NAME%" (
    echo [%date% %time%] Starting automated copy process... > "%LOG_FILE%"
    
    :: Robocopy command: /E (all subfolders), /IS (overwrite identical), /IT (overwrite tweaked), /R:3 (retry 3 times if locked), /W:1 (wait 1 sec between retries)
    robocopy "%SOURCE_DIR%" "%DEST_DIR%" /E /IS /IT /R:3 /W:1 >> "%LOG_FILE%" 2>&1
    
    echo [%date% %time%] Script finished. >> "%LOG_FILE%"
) else (
    echo [%date% %time%] Error: Folder mismatch! > "%LOG_FILE%"
)