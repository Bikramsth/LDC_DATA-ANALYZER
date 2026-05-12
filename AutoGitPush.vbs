' AutoGitPush.vbs
Set objShell = WScript.CreateObject("WScript.Shell")

' --- CONFIGURATION ---
' Set your repository path here
strRepoPath = "C:\Users\CRLDC\Documents\GitHub\LDC_DATA-ANALYZER" 
' ---------------------

Do
    ' Change directory to the repository
    objShell.CurrentDirectory = strRepoPath
    
    ' Format the current date and time for the commit message
    commitMessage = "Auto-commit updated files at " & Now
    
    ' Build the Git command: add all, commit, and push to main
    strCommand = "cmd.exe /c git add . && git commit -m """ & commitMessage & """ && git push origin main"
    
    ' Execute the command silently (0 means hidden window) and wait for it to finish (True)
    objShell.Run strCommand, 0, True
    
    ' Pause the script for 30 minutes
    ' (30 minutes * 60 seconds * 1000 milliseconds = 1,800,000 ms)
    WScript.Sleep 1800000
Loop