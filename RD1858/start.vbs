Set WshShell = CreateObject("WScript.Shell")
Set fso      = CreateObject("Scripting.FileSystemObject")
Set http     = CreateObject("WinHttp.WinHttpRequest.5.1")

ProjectDir = "C:\durgesh\RD1858"

' Find python.exe
Dim pyExe, candidates(7), i
candidates(0) = WshShell.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Programs\Python\Python313\python.exe"
candidates(1) = WshShell.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Programs\Python\Python312\python.exe"
candidates(2) = WshShell.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Programs\Python\Python311\python.exe"
candidates(3) = WshShell.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Programs\Python\Python310\python.exe"
candidates(4) = "C:\Python313\python.exe"
candidates(5) = "C:\Python312\python.exe"
candidates(6) = "C:\Python311\python.exe"
candidates(7) = "C:\Python310\python.exe"

pyExe = "python"
For i = 0 To 7
    If fso.FileExists(candidates(i)) Then
        pyExe = candidates(i)
        Exit For
    End If
Next

' Write a small launcher batch file that sets encoding then runs python
Dim batPath
batPath = ProjectDir & "\launch.bat"
Dim f
Set f = fso.CreateTextFile(batPath, True)
f.WriteLine "@echo off"
f.WriteLine "set PYTHONIOENCODING=utf-8"
f.WriteLine "set PYTHONUTF8=1"
f.WriteLine "cd /d """ & ProjectDir & """"
f.WriteLine """" & pyExe & """ """ & ProjectDir & "\run.py"" >> """ & ProjectDir & "\bot.log"" 2>&1"
f.Close

' Run the batch file hidden
WshShell.Run "cmd /c """ & batPath & """", 0, False

' Poll until Flask is up (max 60s)
Dim ready, waited
ready  = False
waited = 0
Do While Not ready And waited < 60
    WScript.Sleep 1000
    waited = waited + 1
    On Error Resume Next
    http.Open "GET", "http://localhost:5000/ping", False
    http.SetTimeouts 1000, 1000, 1000, 1000
    http.Send
    If Err.Number = 0 Then
        If http.Status = 200 Then ready = True
    End If
    On Error GoTo 0
Loop

' FIX: WshShell.Run "http://localhost:5000", 1 has been completely removed 
' to let algo.bat handle browser launch isolation with no duplicate tab spawning.

If Not ready Then
    MsgBox "Server slow to start. If browser fails, check: " & ProjectDir & "\bot.log", 48, "Wealth++ Bot"
End If