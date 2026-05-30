Wealth++ Algo Setup

use ulaa or chrome browser
Install Python 3.11 windows
Copy the entire RD1858 folder to the new machine. change ac name RD1858.
Python Package Install Commands------------
pip install flask==3.1.0 flask-cors==4.0.0 requests==2.32.3 pyotp==2.9.0 pandas>=2.0.0 numpy==1.26.2 kiteconnect==5.0.1 python-dateutil==2.8.2 pytz>=2024.1 tabulate==0.9.0

python -c "import flask, flask_cors, requests, pyotp, pandas, numpy, kiteconnect, dateutil, pytz, tabulate; print('All packages OK')"

pip install -r C:\durgesh\RD1858\requirements.txt

Files With Hardcoded Physical Paths to Edit------------
1. start.vbs
| 5 | `ProjectDir = "C:\durgesh\RD1858"` | Full path to the new folder, e.g. `"C:\Users\John\RD1858"` |

2. algo.bat (note: for single instance use algo-single instance.bat)
| 24 | `start "" wscript.exe "C:\durgesh\RD1858\start.vbs"` | Path to this account's `start.vbs` |
edit browser ulaa/chrome installation paths

3. edit algo sched.xml in notepad---
<!-- Change this line: -->
<Arguments>/c start "" "C:\durgesh\RD1858\algo.bat"</Arguments>
<WorkingDirectory>C:\durgesh</WorkingDirectory>
<!-- To: -->
<Arguments>/c start "" "C:\Users\John\RD1858\algo.bat"</Arguments>
<WorkingDirectory>C:\Users\John\RD1858</WorkingDirectory>
run task scheduler & import this xml

4. disable TOTP & re-enable it to copy totp_key & save in in \config\credentials.json

5. create desktop shortcut for algo.bat & run it. ensure existing ulaa/chrome browsers are closed before running this bat. 

6. you can login to kite after clicking on 'Pause bot' in the algo dashboard.
