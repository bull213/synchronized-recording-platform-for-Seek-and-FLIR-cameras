# Camera Drivers and Device Manager Setup Guide

This guide covers installing and verifying the camera drivers for the **Seek Thermal** and **FLIR USB3** cameras on a fresh or secondary Windows 10/11 x64 PC.

---

## Bundled Drivers

The repository and the exported transfer ZIP bundle the required drivers directly in the `drivers/` folder:

| Camera | Driver Location | Key Files | Description |
| --- | --- | --- | --- |
| **Seek Thermal** | `drivers/Seek/` | `seekusb.inf`, `seekusb.cat` | Official WinUSB driver for Seek Compact / CompactPRO / Micro-Core cameras (`VID_289D & PID_0010`, `PID_0011`, `PID_FFF2`). |
| **FLIR USB3** | `drivers/FLIR_USB3/` | `PGRUSBCam3.inf`, `pgrusbcam3.cat`, `PGRUSBCam3.sys`, `WdfCoInstaller01009.dll` | Teledyne FLIR Point Grey USB 3.0 Vision driver (`VID_1E10 & PID_4000`, etc.). |

---

## Method 1: Automatic Installation (Recommended)

1. Open the project root or the extracted `SyncRecorder` folder.
2. **Right-click `Install_Camera_Drivers.cmd`** and select **Run as administrator**.
3. The script registers both `drivers\Seek\seekusb.inf` and `drivers\FLIR_USB3\PGRUSBCam3.inf` into the Windows DriverStore using `pnputil.exe`.
4. If prompted to reboot (exit code 3010), restart Windows.
5. Plug in the Seek and FLIR cameras into dedicated USB 3.0 ports.

---

## Method 2: Manual Update via Windows Device Manager

If Windows does not automatically bind the driver when the Seek Thermal camera is plugged in, or if it appears in Device Manager under **Other devices** with a yellow exclamation mark:

1. Connect the Seek Thermal camera to a USB port.
2. Press **Win + X** on your keyboard and click **Device Manager** (or press **Win + R**, type `devmgmt.msc`, and press Enter).
3. Look for the camera:
   - Under **Other devices** &rarr; **Seek Thermal Camera**, or
   - Under **Universal Serial Bus devices** &rarr; **Seek Thermal Camera**, or
   - Under **Cameras** / **Imaging devices**.
4. **Right-click** on the device and select **Update driver**.
5. Select **Browse my computer for drivers**.
6. Click **Browse...** and navigate to the `drivers\Seek` directory inside your extracted project or ZIP folder, e.g.:
   ```
   C:\path\to\SyncRecorder\drivers\Seek
   ```
7. Make sure **Include subfolders** is checked and click **Next**.
8. Windows will install `seekusb.inf` and bind the device to WinUSB.
9. In Device Manager, the camera should now appear under **Universal Serial Bus devices** as **Seek Thermal Camera** with no warning icon.

---

## Verifying Camera Detection

After driver installation:

1. **Verify via Self-Check**:
   Double-click `Setup_New_PC.cmd` (or run `Start_Sync_Recorder.cmd --check-setup`).
   - Confirm that Seek and FLIR devices are detected without errors.
2. **Launch the Recorder**:
   Double-click `Start_Sync_Recorder.cmd`.
   - The Seek thermal preview and FLIR camera views should stream live in the main window.
3. **If FLIR Camera is Not Detected**:
   - Verify that the camera is connected to a native USB 3.0 port (blue port).
   - In Device Manager, the FLIR camera should appear under **Point Grey Research Devices** or **Cameras** as **Point Grey USB3 Camera**.
   - For GigE FLIR cameras, install the Teledyne Spinnaker SDK and use SpinView to configure the network adapter IP.
