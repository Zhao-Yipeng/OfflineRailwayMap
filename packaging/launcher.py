"""PyInstaller entry script for the desktop program."""

import sys

# Never write .pyc files next to the program (SPEC §11.2): everything the
# program writes must be inside data\, which 「清空缓存」 deletes.
sys.dont_write_bytecode = True

if "--self-check" in sys.argv:  # CI: check the build's libraries (SPEC §17.1)
    from offline_railway_map.selfcheck import main as self_check

    sys.exit(self_check(sys.argv))

from offline_railway_map.app import main  # noqa: E402

sys.exit(main())
