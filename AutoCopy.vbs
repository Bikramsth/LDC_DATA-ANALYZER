Option Explicit

Dim fso, sourceFolder, destFolder, file, ext
Set fso = CreateObject("Scripting.FileSystemObject")

' --- CONFIGURATION ---
' Make sure these paths are correct for your machine
sourceFolder = "D:\DATA\0 Data_Collection 75\Daily_Log_Sheets\Log Sheet 2083"
destFolder = "C:\Users\CRLDC\Documents\GitHub\LDC_DATA-ANALYZER\LDC_Data\Log Sheet 2083"
' ---------------------

' Ensure the destination folder exists
If Not fso.FolderExists(destFolder) Then
    fso.CreateFolder(destFolder)
End If

' Loop through files in the source folder
For Each file In fso.GetFolder(sourceFolder).Files
    ext = LCase(fso.GetExtensionName(file.Name))
    
    ' Check if the extension starts with "xl" (xlsx, xls, xlsm, etc.)
    If Left(ext, 2) = "xl" Then
        ' The "True" parameter forces the automatic replace/overwrite
        fso.CopyFile file.Path, destFolder & "\", True
    End If
Next

MsgBox "Excel files copied and replaced successfully!", vbInformation, "Done"

Set fso = Nothing