# Trading through NinjaTrader 8 with PropTradeBot

A small NinjaTrader add-on, **PTBBridge**, connects PropTradeBot to NinjaTrader 8. It places PropTradeBot's orders in
NinjaTrader and enforces its own hard limits, even if PropTradeBot stops.

**Before you start:** confirm that your prop firm allows automated trading through NinjaTrader 8 on your accounts.
That's your responsibility. Setup asks you to confirm it, and PropTradeBot does not check it for you.

There are two ways to set it up. Pick the one that matches your computers:

- **[PropTradeBot and NinjaTrader on the same Windows PC](#same-pc)**: the simple one. No addresses, no firewall,
  about 10 minutes.
- **[PropTradeBot on a Mac, NinjaTrader on Windows](#mac-and-windows)**: a separate Windows PC or a Windows virtual
  machine on your Mac. About 15 minutes and a Windows administrator login.

---

<a id="same-pc"></a>
# PropTradeBot and NinjaTrader on the same Windows PC

You'll need NinjaTrader 8 installed and logged in, and PropTradeBot for Windows installed on the same PC.
Nothing leaves the PC: the add-on only accepts connections from the PC it runs on.

## 1. Create the add-on's settings file

Create the file `Documents\NinjaTrader 8\ptbbridge.cfg`. Start from `ptbbridge.cfg.example`, which comes with this
guide, and change:

| Setting | Set it to |
|---|---|
| `accounts=` | your NinjaTrader account name(s), exactly as on NinjaTrader's **Accounts** tab |
| `instruments=` | `MES`, or `MES,MNQ` if you also trade the Nasdaq leg |
| `max_order_qty=` and `max_position_qty=` | at least your **Contracts** setting in PropTradeBot |
| `daily_loss_limit=` | a dollar loss per account per day. When an account reaches it, the add-on **closes that account's position** and refuses new entries for the rest of the session (`0` = off) |

Then **delete the `listen=` and `allow_from=` lines** at the bottom. They're only for a Mac on another machine.
Without them the add-on accepts connections from this PC only.

Windows may save the file as `ptbbridge.cfg.txt`. In File Explorer turn on **View → Show → File name extensions** to
check, and rename it if so.

## 2. Install the add-on in NinjaTrader

1. Download **PTBBridge.zip** to the Windows PC. Don't unzip it.
2. In NinjaTrader's Control Center: **Tools → Import → NinjaScript Add-On…**
3. Choose `PTBBridge.zip` and click **Open**.
4. NinjaTrader warns that the archive "contains NinjaScript add ons which are run automatically" and asks whether
   to authorize them. Click **Yes**.
5. NinjaTrader then asks you to shut down and restart it "for the assembly PTBBridge to take effect". Close
   NinjaTrader completely and start it again.

**Is your Documents folder in OneDrive?** (Common on Windows 11 with a Microsoft account: the path to
`Documents\NinjaTrader 8` contains `OneDrive`.) Then Import fails with "Import failed. The NinjaScript Archive File
may contain duplicate method names…" and NinjaTrader may close. That message is misleading: OneDrive keeps
NinjaTrader's folders read-only, so the import can't finish. Install by hand instead:

1. Copy `PTBBridge.cs` into `Documents\NinjaTrader 8\bin\Custom\AddOns\`.
2. In NinjaTrader: **New → NinjaScript Editor**. In the *NinjaScript Explorer* on the right, double-click **AddOns**,
   then **PTBBridge**.
3. Right-click in the code → **Compile**. When it finishes with no errors at the bottom, close the editor.

OneDrive also copies the add-on's token file (`ptbbridge_token.txt`, section 3) to your Microsoft cloud. If that matters to you, keep
NinjaTrader's folder out of OneDrive.

Until the settings file from section 1 is in place, the Log shows "PTBBridge FAILED to start: missing
…ptbbridge.cfg (no accounts allowed without it)". That's expected: without settings it refuses to trade.

Check it started: in NinjaTrader's Control Center open the **Log** tab and look for

```
PTBBridge listening on 127.0.0.1:7811 allow_from=(this PC only) accounts=... roots=MES ...
```

The add-on starts by itself every time NinjaTrader starts.

## 3. Connect PropTradeBot

Open PropTradeBot. Its setup opens the first time.

1. Firm: **I trade through NinjaTrader 8 (any firm)**, and tick the confirmation.
2. Setup fills in **Add-on address** `127.0.0.1` (this PC) and **Port** `7811`. Keep both.
3. Setup finds the add-on's token on this PC by itself and shows **"Found the add-on's token"**. The token is a
   private key the add-on created in `Documents\NinjaTrader 8\ptbbridge_token.txt`. PropTradeBot reads it from
   there; you don't need to copy it. If setup asks for the token instead, NinjaTrader hasn't started the add-on yet
   (section 2), or open that file and paste the token.
4. **Connect & Find My Accounts**, then pick the account(s) PropTradeBot should trade.
5. **Flatten my NinjaTrader accounts at:** set it before your firm's cutoff, with a margin. It's required and has no
   default. At that time PropTradeBot closes **every position on those accounts**, not only its own trades, and takes
   no new entries until the 6:00 PM ET reopen.
6. Set your strategy and **Save & Restart**.

Both NinjaTrader and PropTradeBot must stay running, and NinjaTrader logged in, while PropTradeBot trades. If the
connection drops, PropTradeBot stops taking entries, then rebuilds any open trade from NinjaTrader's own positions
when it reconnects.

The window's close button only minimises PropTradeBot to the taskbar; it keeps trading. To update or uninstall it,
first make sure no trade is open, then choose **Quit PropTradeBot** in its menu. The installer and uninstaller refuse
to run while it's open. Uninstalling keeps your settings and trade history.

**If it doesn't connect,** see [If it doesn't connect](#if-it-doesnt-connect) below. On the same PC the usual causes
are an address other than `127.0.0.1`, NinjaTrader not started since the add-on was installed, or NinjaTrader not running.

---

<a id="mac-and-windows"></a>
# PropTradeBot on a Mac, NinjaTrader on Windows

PropTradeBot runs on your Mac. NinjaTrader 8 runs on Windows, either a separate Windows PC or a Windows virtual
machine on your Mac.

You'll need about 15 minutes, a Windows administrator login (for one firewall rule), and NinjaTrader 8 logged in.

## 1. Find the two addresses

You'll need two addresses, one for each machine.

- **The Windows PC's address.** On Windows, open *Command Prompt* and run `ipconfig`. Use the **IPv4 Address** of the
  network adapter that's connected (for example `192.168.1.20`).
- **Your Mac's address.** Open *System Settings → Wi-Fi* (or *Network*), click **Details…** on your connection. The
  **IP address** is on the TCP/IP tab (for example `192.168.1.15`).

Home routers can hand out new addresses after a restart. To stop that, reserve both addresses in your router's
DHCP settings (often called "DHCP reservation" or "static lease"). Otherwise, if one changes, repeat the steps below
that use it.

## 2. Create the add-on's settings file

On the Windows PC, create the file `Documents\NinjaTrader 8\ptbbridge.cfg`. Start from `ptbbridge.cfg.example`, which
comes with this guide, and change:

| Setting | Set it to |
|---|---|
| `accounts=` | your NinjaTrader account name(s), exactly as on NinjaTrader's **Accounts** tab |
| `instruments=` | `MES`, or `MES,MNQ` if you also trade the Nasdaq leg |
| `max_order_qty=` and `max_position_qty=` | at least your **Contracts** setting in PropTradeBot |
| `daily_loss_limit=` | a dollar loss per account per day. When an account reaches it, the add-on **closes that account's position** and refuses new entries for the rest of the session (`0` = off) |
| `listen=` | **the Windows PC's address** from step 1 |
| `allow_from=` | **your Mac's address** from step 1 |

The add-on will not start without this file. If `listen=` is set but `allow_from=` is missing, it also refuses to
start, so the port is never open to everyone.

## 3. Allow the connection through Windows Firewall

Open *Command Prompt* **as administrator** (right-click → Run as administrator) and run, with your Mac's address:

```
netsh advfirewall firewall add rule name="PropTradeBot add-on" dir=in action=allow protocol=TCP localport=7811 remoteip=192.168.1.15
```

It should print `Ok.` This lets only your Mac reach port 7811. If your Mac's address changes later, update the rule:

```
netsh advfirewall firewall set rule name="PropTradeBot add-on" new remoteip=NEW.MAC.ADDRESS
```

**Never forward port 7811 on your router.** The add-on is for your own local network only.

## 4. Install the add-on in NinjaTrader

1. Download **PTBBridge.zip** to the Windows PC. Don't unzip it.
2. In NinjaTrader's Control Center: **Tools → Import → NinjaScript Add-On…**
3. Choose `PTBBridge.zip` and click **Open**.
4. NinjaTrader warns that the archive "contains NinjaScript add ons which are run automatically" and asks whether
   to authorize them. Click **Yes**.
5. NinjaTrader then asks you to shut down and restart it "for the assembly PTBBridge to take effect". Close
   NinjaTrader completely and start it again.

**Is your Documents folder in OneDrive?** (Common on Windows 11 with a Microsoft account: the path to
`Documents\NinjaTrader 8` contains `OneDrive`.) Then Import fails with "Import failed. The NinjaScript Archive File
may contain duplicate method names…" and NinjaTrader may close. That message is misleading: OneDrive keeps
NinjaTrader's folders read-only, so the import can't finish. Install by hand instead:

1. Copy `PTBBridge.cs` into `Documents\NinjaTrader 8\bin\Custom\AddOns\`.
2. In NinjaTrader: **New → NinjaScript Editor**. In the *NinjaScript Explorer* on the right, double-click **AddOns**,
   then **PTBBridge**.
3. Right-click in the code → **Compile**. When it finishes with no errors at the bottom, close the editor.

OneDrive also copies the add-on's token file (section 5) to your Microsoft cloud. If that matters to you, keep
NinjaTrader's folder out of OneDrive.

Until the settings file from section 2 is in place, the Log shows "PTBBridge FAILED to start: missing
…ptbbridge.cfg (no accounts allowed without it)". That's expected: without settings it refuses to trade.

Check it started: in NinjaTrader's Control Center open the **Log** tab and look for

```
PTBBridge listening on 192.168.1.20:7811 allow_from=192.168.1.15 accounts=... roots=MES ...
```

with your addresses. The add-on starts by itself every time NinjaTrader starts.

## 5. Get the add-on token

The first time it starts, the add-on creates a random token in `Documents\NinjaTrader 8\ptbbridge_token.txt` on the
Windows PC. Open the file and copy the token. PropTradeBot needs it to connect. Keep it private; anyone with the token
and network access to the PC could send orders through the add-on.

## 6. Connect PropTradeBot

On your Mac, open PropTradeBot's setup:

1. Firm: **I trade through NinjaTrader 8 (any firm)**, and tick the confirmation.
2. **Add-on address:** the Windows PC's address from step 1. **Port:** `7811`. **Token:** from step 5.
3. **Connect & Find My Accounts**, then pick the account(s) PropTradeBot should trade.
4. **Flatten my NinjaTrader accounts at:** set it before your firm's cutoff, with a margin. It's required and has no
   default. At that time PropTradeBot closes **every position on those accounts**, not only its own trades, and takes
   no new entries until the 6:00 PM ET reopen.
5. Set your strategy and **Save & Restart**.

NinjaTrader must stay running and logged in while PropTradeBot trades. If the connection drops, PropTradeBot stops
taking entries, then rebuilds any open trade from NinjaTrader's own positions when it reconnects.

---

## If it doesn't connect

| What you see | What it means | Fix |
|---|---|---|
| NinjaTrader Log: `PTBBridge refused a connection from 192.168.1.37 (not in allow_from)` | A machine at that address tried to connect but isn't in `allow_from=`. Usually your Mac's address changed | Put the address from the log line in `allow_from=` (and in the firewall rule), then restart NinjaTrader |
| NinjaTrader Log: `PTBBridge FAILED to start: listen=... needs allow_from=` | `listen=` is set without `allow_from=` | Add `allow_from=` with your Mac's address |
| NinjaTrader Log: `PTBBridge FAILED to start: missing ...ptbbridge.cfg` | The settings file is missing or misnamed (Windows may have added `.txt`) | Create `Documents\NinjaTrader 8\ptbbridge.cfg` (step 2) |
| NinjaTrader Log: `PTBBridge FAILED to start: unknown key in ptbbridge.cfg: ...` | A typo in a setting name | Fix the line it names |
| PropTradeBot setup can't connect, and nothing appears in NinjaTrader's Log | The connection never reached NinjaTrader: wrong address, firewall rule missing, or the two machines aren't on the same network | Check the address against `ipconfig`, re-run the firewall command in step 3, and make sure both machines are on the same home network |
| PropTradeBot says the token was refused | The token doesn't match `ptbbridge_token.txt` | Copy the token again (step 5) |

## Good to know

- The token travels unencrypted on your local network. That's normal for local trading tools, but only use this on a
  network you trust (your home network, not public Wi-Fi), and keep `allow_from=` to your Mac's address.
- The add-on's limits (`max_order_qty`, `max_position_qty`, `daily_loss_limit`) are a second safety net beside
  PropTradeBot's own. They hold even if PropTradeBot misbehaves.
- Only the stop rests at NinjaTrader. PropTradeBot fires its targets from NinjaTrader's live price, so you'll see one
  working stop order per trade and no resting target orders. That's by design.
