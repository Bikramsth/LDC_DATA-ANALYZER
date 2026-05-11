Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

' List of common places Git might be hiding
Dim paths(3)
paths(0) = "C:\Program Files\Git\cmd\git.exe"
paths(1) = shell.ExpandEnvironmentStrings("%LocalAppData%\GitHubDesktop\bin\git.exe")
paths(2) = shell.ExpandEnvironmentStrings("%LocalAppData%\Programs\Git\cmd\git.exe")
paths(3) = "C:\Program Files (x86)\Git\cmd\git.exe"

gitPath = ""
For Each p In paths
    If fso.FileExists(p) Then
        gitPath = p
        Exit For
    End If
Next

' FOLDER PATH
folderPath = "C:\Users\CRLDC\Documents\GitHub\LDC_DATA-ANALYZER"

' If we still can't find it, we stop and tell you
If gitPath = "" Then
    MsgBox "Git could not be found automatically. Please download Git from git-scm.com or install GitHub Desktop.", 16, "Git Missing"
    WScript.Quit
End If

' Start the loop
Do
    shell.CurrentDirectory = folderPath
    ' Silent Run: 0 = hide window, True = wait for finish
    shell.Run Chr(34) & gitPath & Chr(34) & " pull origin main", 0, True
    shell.Run Chr(34) & gitPath & Chr(34) & " add .", 0, True
    shell.Run Chr(34) & gitPath & Chr(34) & " commit -m " & Chr(34) & "Auto-sync: " & Now & Chr(34), 0, True
    shell.Run Chr(34) & gitPath & Chr(34) & " push origin main", 0, True
    
    WScript.Sleep 300000 ' 5 minutes
Loop