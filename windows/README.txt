Claude Isolate for Windows x64 (Windows 10 1903 or newer)

Double-click Claude Isolate.exe. Keep the adjacent _internal folder and
Claude Isolate Core.exe together when using the portable ZIP.
The separate *-setup.exe installs the app and a Start Menu shortcut per user.
Python is included. No Python or command-line setup is required.

Click Start to prepare and boot Linux. Python, the tested QEMU x64 runtime,
and portable GnuPG are included. No winget, Microsoft Store, administrator
consent, WSL, Hyper-V installation or manual Windows feature setup is needed.
Keep the runtime folder with the portable ZIP's executables.
Ubuntu is downloaded and verified with the pinned Ubuntu signing key. The
guest installs XFCE, Claude Desktop, and Firefox automatically on its first boot.

Data, sessions, and logs are in %LOCALAPPDATA%\Claude Isolate. Uninstalling the
controller preserves that directory. Do not delete it if you need your VM data.
Close the environment and controller before installing an update. The installer
updates the EXISTING Linux disk, including guests with cloud-init disabled;
it preserves files, accounts, browser profiles, and desktop preferences.
A qcow2 snapshot protects the disk during the offline update. Interrupted
updates are recovered before Linux can boot. The helper has no network adapter.
Claude and Firefox update through the protected gateway on the next guest boot;
if networking is unavailable, the update retries automatically.
Installer progress and errors: %LOCALAPPDATA%\Claude Isolate\installer-update.log.
Portable ZIP upgrades use the same mechanism automatically on the next Start.
The updater does not perform an Ubuntu release upgrade or restore data from
other, inactive disks created by earlier versions.
Use a full-tunnel/TUN VPN; a Windows HTTP proxy alone does not route the gateway.
Synthetic VPN DNS answers in 198.18.0.0/15 are resolved to real public addresses
using certificate-verified Cloudflare DNS-over-HTTPS over the host's routing.
Local addresses remain blocked. If secure DNS cannot be reached, access stays
closed and the controller displays the DNS error.
Automatic resources allocate 2-3 GB RAM and up to 2 virtual CPUs, keeping
memory available for Windows. The host needs at least 4 GB RAM; 8 GB or more
is recommended. Close other applications if available memory is insufficient.
Manual economy (3 GB/2 CPUs) and standard (6 GB/4 CPUs) profiles are optional.
The launcher uses existing WHPX acceleration when available, otherwise software
emulation automatically. No Windows features are enabled or security settings
changed. Software emulation is slower, especially during first installation.
The guest display supports Ctrl+Alt+F for full screen. Supported builds target
Windows 10 1903+ / Windows 11 x64; Windows 7 and 32-bit Windows are unsupported.

Network protection uses periodic IP checks on Windows. It does not guarantee
instant isolation after VPN disconnection or prevent account restrictions.
This is an experimental release; a CI boot/network check does not replace
validation of every Windows/VPN/hardware configuration.

Project source and details: https://github.com/JFT-git/Claude-isolate
This is an independent project, not an official Anthropic distribution.
The Windows executable is not Authenticode-signed. Verify its source and the
published SHA256. Do not disable Windows security globally.
