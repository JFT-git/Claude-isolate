Claude Isolate for Windows x64 (Windows 10 1903 or newer)

Double-click Claude Isolate.exe. Keep the adjacent _internal folder and
Claude Isolate Core.exe together when using the portable ZIP.
The separate *-setup.exe installs the app and a Start Menu shortcut per user.
Python is included. No Python or command-line setup is required.

Click the Start button to prepare and boot Linux. If QEMU or GnuPG is missing,
the app installs QEMU through Microsoft's winget source and downloads a hash-pinned
private GnuPG from the official vendor. Windows may show an
administrator consent prompt for these dependencies. The App Installer package
(winget) must be available; update it in Microsoft Store if necessary.
Ubuntu is downloaded and verified with the pinned Ubuntu signing key. The
guest installs XFCE, Claude Desktop, and Firefox automatically on its first boot.

Data, sessions, and logs are in %LOCALAPPDATA%\Claude Isolate. Uninstalling the
controller preserves that directory. Do not delete it if you need your VM data.
Updating the controller does not require recreating the Linux disk.
Use a full-tunnel/TUN VPN; a Windows HTTP proxy alone does not route the gateway.
Synthetic VPN DNS answers in 198.18.0.0/15 are resolved to real public addresses
using certificate-verified Cloudflare DNS-over-HTTPS over the host's routing.
Local addresses remain blocked. If secure DNS cannot be reached, access stays
closed and the controller displays the DNS error.
The default allocation is 3 GB RAM and 2 CPUs. Enable Windows Hypervisor Platform
and hardware virtualization for fast WHPX acceleration. Without it, the app uses
slower software emulation. The guest display supports Ctrl+Alt+F for full screen.

Network protection uses periodic IP checks on Windows. It does not guarantee
instant isolation after VPN disconnection or prevent account restrictions.
This is an experimental release; a CI boot/network check does not replace
validation of every Windows/VPN/hardware configuration.

Project source and details: https://github.com/JFT-git/Claude-isolate
This is an independent project, not an official Anthropic distribution.
The Windows executable is not Authenticode-signed. Verify its source and the
published SHA256. Do not disable Windows security globally.
