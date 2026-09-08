OpenTelegramStorage for Windows (portable, x64)
================================================

Nothing to install first: Python and every dependency are inside this folder.

  install.cmd              - install to %LOCALAPPDATA%\OpenTelegramStorage,
                             create Start Menu/Desktop shortcuts, optional
                             autostart at login, then start it
  OpenTelegramStorage.cmd  - just run it from here (portable mode)
  uninstall.cmd            - remove program files and shortcuts (keeps data)

After starting, a browser tab opens at http://localhost:8080 with the setup
wizard. Your data (database, keys, staging) is in the "data" folder next to
this file, or in %LOCALAPPDATA%\OpenTelegramStorage\data after install.
Back that folder up.

Other port: set OTS_PORT before starting, e.g.  set OTS_PORT=9000
Server-side import folder: set OTS_IMPORT_DIR=D:\some\folder

Windows Defender SmartScreen may warn about running .cmd files downloaded
from the internet: choose "More info" -> "Run anyway".
