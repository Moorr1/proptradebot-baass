// PTBBridge — NT8 add-on: thin, guarded executor for PropTradeBot (P1, 2026-10-13). Design: nt8_bridge/DESIGN.md.
// PTB owns the ladder; this add-on places/changes/cancels orders, streams prices and order events, and enforces the
// invariant "resting orders at the broker can never open a position" plus hard limits that hold even if PTB is dead.
//
// Config: Documents\NinjaTrader 8\ptbbridge.cfg (key=value lines). Token: Documents\NinjaTrader 8\ptbbridge_token.txt.
// Network (2026-10-15): listens on 127.0.0.1 unless the cfg says listen=<this PC's IPv4> (or 0.0.0.0). A non-local listen
// REQUIRES allow_from=<the Mac's IP or subnet,...>; any other address is closed before a byte is read. This PC itself (127.0.0.1 or its own address) is
// always allowed. Both apply on `reload` (the new listener starts before the old one stops; clients stay connected).
// Protocol: one command per line, one JSON object per reply; events pushed to every authenticated client.
// Instruments are written with '_' for spaces (MES_12-26). Order names are chosen by PTB: ptb-<chars>, unique per account.
//   hello <token>
//   ping                                   -> pong + status counts (PTB heartbeat, ~1s)
//   status                                 -> counts + realized/unrealized P&L + limits + CME session day
//   reload                                 re-read ptbbridge.cfg (all-or-nothing; a bad file keeps the old limits)
//   subscribe <instrument>                 -> tick events (Last/Bid/Ask)
//   positions <account> | orders <account> | reconcile <account>
//   enter <account> <instrument> BUY|SELL <qty> <name>          market entry, flat or same side only
//   stop <account> <instrument> <stopPrice> <qty> <name>        the ONE resting exit, closing side, qty <= position
//   change_stop <account> <name> <qty> <stopPrice>
//   exit <account> <instrument> <qty> <name>                    market partial exit; stop + qty must be <= position
//   flatten <account> <instrument>                              Account.Flatten: cancels working orders, closes position
//   cancel <account> <name>
//   block <account> <reason...> | unblock <account>            PTB-set entry block (e.g. reconcile not passed)
#region Using declarations
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using NinjaTrader.Cbi;
using NinjaTrader.Data;
#endregion

namespace NinjaTrader.NinjaScript.AddOns
{
	public class PTBBridge : AddOnBase
	{
		const int Port = 7811;
		static readonly Regex NameRx = new Regex("^ptb-[A-Za-z0-9-]{1,48}$");

		// config
		HashSet<string> allowedAccounts = new HashSet<string>();
		HashSet<string> allowedRoots = new HashSet<string>();
		int maxOrderQty = 1, maxPositionQty = 1, feedStaleMs = 5000;
		double dailyLossLimit;   // per account, USD; 0 = off
		IPAddress cfgListen = IPAddress.Loopback;            // from ptbbridge.cfg
		IPAddress listenAddr;                                // what the listener is actually bound to
		List<uint[]> allowNets = new List<uint[]>();         // {network, mask}, IPv4; loopback is always allowed
		string allowFromText = "";

		volatile TcpListener listener;
		volatile bool running;
		string token;
		System.Threading.Timer guardTimer;
		readonly List<Client> clients = new List<Client>();
		readonly HashSet<Account> hooked = new HashSet<Account>();
		readonly Dictionary<string, MarketData> feeds = new Dictionary<string, MarketData>();     // instrument full name
		readonly Dictionary<string, DateTime> lastTickUtc = new Dictionary<string, DateTime>();
		readonly Dictionary<string, double> lastPrice = new Dictionary<string, double>();
		readonly Dictionary<string, string> blocks = new Dictionary<string, string>();            // account -> reason
		readonly Dictionary<string, string> lossBlocks = new Dictionary<string, string>();        // account -> reason (daily loss)
		readonly Dictionary<string, string> lossBlockDay = new Dictionary<string, string>();      // account -> CME session day of the block
		string cfgPath;
		DateTime lastBrokerEventUtc = DateTime.MinValue, lastPingUtc = DateTime.MinValue;
		readonly object sync = new object();

		class Client { public TcpClient Tcp; public StreamWriter Writer; public bool Authed; }

		protected override void OnStateChange()
		{
			if (State == State.SetDefaults)
			{
				Name = "PTBBridge";
				Description = "PropTradeBot bridge: guarded executor (one resting exit, hard limits; network access only from allow_from).";
			}
			else if (State == State.Active) Start();
			else if (State == State.Terminated) Stop();
		}

		// ---------------------------------------------------------------- lifecycle
		void Start()
		{
			try
			{
				string dir = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments), "NinjaTrader 8");
				string tokenFile = Path.Combine(dir, "ptbbridge_token.txt");
				if (!File.Exists(tokenFile)) File.WriteAllText(tokenFile, Guid.NewGuid().ToString("N"));
				token = File.ReadAllText(tokenFile).Trim();
				cfgPath = Path.Combine(dir, "ptbbridge.cfg");
				LoadConfig(cfgPath);

				listener = new TcpListener(cfgListen, Port);
				listener.Start();
				listenAddr = cfgListen;
				running = true;
				new Thread(AcceptLoop) { IsBackground = true, Name = "PTBBridgeAccept" }.Start();
				guardTimer = new System.Threading.Timer(_ => GuardAll("timer"), null, 1000, 1000);
				Report("PTBBridge listening on " + listenAddr + ":" + Port + " allow_from=" + (allowFromText.Length > 0 ? allowFromText : "(this PC only)")
					+ " accounts=" + string.Join(",", allowedAccounts)
					+ " roots=" + string.Join(",", allowedRoots) + " max_order=" + maxOrderQty + " max_pos=" + maxPositionQty
					+ " daily_loss=" + N(dailyLossLimit) + " feed_stale_ms=" + feedStaleMs, LogLevel.Information);
			}
			catch (Exception ex) { Report("PTBBridge FAILED to start: " + ex.Message, LogLevel.Error); }
		}

		void LoadConfig(string path)
		{
			if (!File.Exists(path))
				throw new InvalidOperationException("missing " + path + " (no accounts allowed without it)");
			// Parse into locals and swap only if the whole file is valid, so a bad `reload` keeps the old limits.
			HashSet<string> accts = new HashSet<string>(), roots = new HashSet<string>();
			int maxOrder = 1, maxPos = 1, stale = 5000;
			double loss = 0;
			IPAddress listen = IPAddress.Loopback;
			List<uint[]> nets = new List<uint[]>();
			string netsText = "";
			foreach (string raw in File.ReadAllLines(path))
			{
				string line = raw.Trim();
				if (line.Length == 0 || line.StartsWith("#")) continue;
				int eq = line.IndexOf('=');
				if (eq < 0) continue;
				string k = line.Substring(0, eq).Trim().ToLowerInvariant(), v = line.Substring(eq + 1).Trim();
				if (k == "accounts") accts = Csv(v);
				else if (k == "instruments") roots = Csv(v.ToUpperInvariant());
				else if (k == "max_order_qty") maxOrder = int.Parse(v, CultureInfo.InvariantCulture);
				else if (k == "max_position_qty") maxPos = int.Parse(v, CultureInfo.InvariantCulture);
				else if (k == "daily_loss_limit") loss = double.Parse(v, CultureInfo.InvariantCulture);
				else if (k == "feed_stale_ms") stale = int.Parse(v, CultureInfo.InvariantCulture);
				else if (k == "listen")
				{
					if (!IPAddress.TryParse(v, out listen) || listen.AddressFamily != AddressFamily.InterNetwork)
						throw new InvalidOperationException("listen= must be this PC's IPv4 address (e.g. 192.168.1.20), 0.0.0.0 for all, or 127.0.0.1");
				}
				else if (k == "allow_from") { nets = ParseNets(v); netsText = string.Join(",", Csv(v)); }
				else throw new InvalidOperationException("unknown key in ptbbridge.cfg: " + k);
			}
			if (accts.Count == 0 || roots.Count == 0)
				throw new InvalidOperationException("ptbbridge.cfg must list accounts= and instruments=");
			if (maxOrder < 1 || maxPos < maxOrder || stale < 1 || loss < 0)
				throw new InvalidOperationException("ptbbridge.cfg limits invalid (need 1 <= max_order_qty <= max_position_qty, feed_stale_ms >= 1, daily_loss_limit >= 0)");
			if (!IPAddress.IsLoopback(listen) && nets.Count == 0)
				throw new InvalidOperationException("listen=" + listen + " needs allow_from= (your Mac's IP address), so the port is never open to everyone");
			cfgListen = listen; allowNets = nets; allowFromText = netsText;
			allowedAccounts = accts; allowedRoots = roots;
			maxOrderQty = maxOrder; maxPositionQty = maxPos; feedStaleMs = stale; dailyLossLimit = loss;
		}

		static List<uint[]> ParseNets(string v)
		{
			var outp = new List<uint[]>();
			foreach (string part in Csv(v))
			{
				string ip = part; int bits = 32;
				int slash = part.IndexOf('/');
				if (slash >= 0)
				{
					ip = part.Substring(0, slash);
					if (!int.TryParse(part.Substring(slash + 1), NumberStyles.None, CultureInfo.InvariantCulture, out bits)) bits = -1;
				}
				IPAddress a;
				if (!IPAddress.TryParse(ip, out a) || a.AddressFamily != AddressFamily.InterNetwork || bits < 8 || bits > 32)
					throw new InvalidOperationException("allow_from: '" + part + "' is not an IPv4 address or subnet like 192.168.1.15 or 192.168.1.0/24 (/8 to /32)");
				uint mask = 0xFFFFFFFFu << (32 - bits);
				outp.Add(new uint[] { ToUint(a) & mask, mask });
			}
			return outp;
		}

		static uint ToUint(IPAddress a)
		{
			byte[] b = a.GetAddressBytes();
			return ((uint)b[0] << 24) | ((uint)b[1] << 16) | ((uint)b[2] << 8) | b[3];
		}

		bool AllowedPeer(IPAddress a)
		{
			if (a.IsIPv4MappedToIPv6) a = a.MapToIPv4();
			if (IPAddress.IsLoopback(a)) return true;
			if (a.AddressFamily != AddressFamily.InterNetwork) return false;
			uint x = ToUint(a);
			foreach (uint[] n in allowNets) if ((x & n[1]) == n[0]) return true;
			return false;
		}

		static HashSet<string> Csv(string v)
		{
			return new HashSet<string>(v.Split(',').Select(x => x.Trim()).Where(x => x.Length > 0));
		}

		void Stop()
		{
			running = false;
			try { if (guardTimer != null) guardTimer.Dispose(); } catch { }
			try { if (listener != null) listener.Stop(); } catch { }
			lock (clients) { foreach (Client c in clients) { try { c.Tcp.Close(); } catch { } } clients.Clear(); }
			lock (hooked)
			{
				foreach (Account a in hooked) { a.OrderUpdate -= OnOrderUpdate; a.ExecutionUpdate -= OnExecutionUpdate; a.PositionUpdate -= OnPositionUpdate; }
				hooked.Clear();
			}
			lock (feeds)
			{
				foreach (MarketData md in feeds.Values) { try { md.Update -= OnMarketData; } catch { } }
				feeds.Clear();
			}
		}

		// listen= changed on reload: start the new listener, swap it in, then stop the old one. A bad address keeps the
		// bridge reachable where it was; connected clients are not dropped. Returns what happened, or null if unchanged.
		string Rebind()
		{
			if (listenAddr != null && cfgListen.Equals(listenAddr)) return null;
			TcpListener fresh = new TcpListener(cfgListen, Port);
			try { fresh.Start(); }
			catch (Exception ex) { return "could not listen on " + cfgListen + ":" + Port + " (" + ex.Message + ") - still on " + listenAddr; }
			TcpListener old = listener;
			listener = fresh;
			listenAddr = cfgListen;
			try { if (old != null) old.Stop(); } catch { }
			return "now listening on " + listenAddr + ":" + Port;
		}

		void AcceptLoop()
		{
			while (running)
			{
				TcpListener l = listener;
				try
				{
					TcpClient tcp = l.AcceptTcpClient();
					IPAddress from = ((IPEndPoint)tcp.Client.RemoteEndPoint).Address;
					IPAddress self = ((IPEndPoint)tcp.Client.LocalEndPoint).Address;
					if (!from.Equals(self) && !AllowedPeer(from))      // from == self: a program on this PC
					{
						Report("PTBBridge refused a connection from " + from + " (not in allow_from)", LogLevel.Warning);
						try { tcp.Close(); } catch { }
						continue;
					}
					var c = new Client { Tcp = tcp, Writer = new StreamWriter(tcp.GetStream(), new UTF8Encoding(false)) { AutoFlush = true, NewLine = "\n" } };
					lock (clients) clients.Add(c);
					new Thread(() => ClientLoop(c)) { IsBackground = true, Name = "PTBBridgeClient" }.Start();
				}
				catch (Exception ex) { if (running && l == listener) Report("PTBBridge accept error: " + ex.Message, LogLevel.Warning); }
			}
		}

		void ClientLoop(Client c)
		{
			try
			{
				var reader = new StreamReader(c.Tcp.GetStream(), Encoding.UTF8);
				string line;
				while (running && (line = reader.ReadLine()) != null)
				{
					line = line.Trim();
					if (line.Length == 0) continue;
					string reply;
					try { reply = Handle(c, line); }
					catch (Exception ex) { reply = Err(ex.GetType().Name + ": " + ex.Message); }
					Send(c, reply);
				}
			}
			catch { }
			finally { lock (clients) clients.Remove(c); try { c.Tcp.Close(); } catch { } }
		}

		// ---------------------------------------------------------------- commands
		string Handle(Client c, string line)
		{
			string[] p = line.Split(new[] { ' ' }, StringSplitOptions.RemoveEmptyEntries);
			string cmd = p[0].ToLowerInvariant();
			if (cmd == "hello")
			{
				c.Authed = p.Length > 1 && p[1] == token;
				return c.Authed ? Ok("hello", "\"status\":" + StatusJson()) : Err("bad token");
			}
			if (!c.Authed) return Err("not authenticated: send hello <token> first");

			switch (cmd)
			{
				case "ping":
					lastPingUtc = DateTime.UtcNow;
					return Ok("pong", "\"status\":" + StatusJson());
				case "status":
					return Ok("status", "\"status\":" + StatusJson());
				case "reload":
					LoadConfig(cfgPath);
					{
						string rb = Rebind();
						if (rb != null) Report("PTBBridge " + rb, rb.StartsWith("could not") ? LogLevel.Error : LogLevel.Information);
					}
					Report("PTBBridge config reloaded: listen=" + listenAddr + " allow_from=" + (allowFromText.Length > 0 ? allowFromText : "(this PC only)") + " accounts=" + string.Join(",", allowedAccounts) + " roots=" + string.Join(",", allowedRoots)
						+ " max_order=" + maxOrderQty + " max_pos=" + maxPositionQty + " daily_loss=" + N(dailyLossLimit) + " feed_stale_ms=" + feedStaleMs, LogLevel.Information);
					return Ok("reload", "\"limits\":" + LimitsJson());
				case "subscribe":
				{
					Need(p, 2, "subscribe <instrument>");
					Instrument inst = AllowedInstrument(p[1]);
					lock (feeds)
					{
						if (!feeds.ContainsKey(inst.FullName))
						{
							var md = new MarketData(inst);
							md.Update += OnMarketData;
							feeds[inst.FullName] = md;
						}
					}
					return Ok("subscribe", "\"instrument\":" + Q(inst.FullName));
				}
				case "positions":
				{
					Account acct = AllowedAccount(Arg(p, 1));
					return Ok("positions", "\"account\":" + Q(acct.Name) + ",\"positions\":" + PositionsJson(acct));
				}
				case "orders":
				{
					Account acct = AllowedAccount(Arg(p, 1));
					return Ok("orders", "\"account\":" + Q(acct.Name) + ",\"orders\":" + OrdersJson(acct, false));
				}
				case "reconcile":
				{
					// Snapshot for PTB to compare against its journal. The mechanical rules run here first.
					Account acct = AllowedAccount(Arg(p, 1));
					Guard(acct, "reconcile");
					return Ok("reconcile", "\"account\":" + Q(acct.Name) + ",\"connection\":" + Q(acct.ConnectionStatus.ToString())
						+ ",\"positions\":" + PositionsJson(acct) + ",\"working\":" + OrdersJson(acct, true)
						+ ",\"blocked\":" + Q(BlockReason(acct)));
				}
				case "enter":
				{
					Need(p, 6, "enter <account> <instrument> BUY|SELL <qty> <name>");
					Account acct = AllowedAccount(p[1]);
					Instrument inst = AllowedInstrument(p[2]);
					OrderAction action = Side(p[3]);
					int qty = Qty(p[4]);
					string name = NewName(acct, p[5]);
					string why = EntryRefusal(acct, inst);
					if (why != null) return Err("entry refused: " + why);
					int pos = SignedPosition(acct, inst);
					int dir = action == OrderAction.Buy ? 1 : -1;
					if (pos != 0 && Math.Sign(pos) != dir) return Err("entry refused: opposite-side position " + pos + " (no reversals)");
					if (Math.Abs(pos) + qty > maxPositionQty) return Err("entry refused: position would be " + (Math.Abs(pos) + qty) + " > max_position_qty " + maxPositionQty);
					return Submit(acct, inst, action, OrderType.Market, qty, 0, name);
				}
				case "stop":
				{
					Need(p, 6, "stop <account> <instrument> <stopPrice> <qty> <name>");
					Account acct = AllowedAccount(p[1]);
					Instrument inst = AllowedInstrument(p[2]);
					double px = Price(p[3]);
					int qty = Qty(p[4]);
					string name = NewName(acct, p[5]);
					int pos = SignedPosition(acct, inst);
					if (pos == 0) return Err("stop refused: flat (a resting stop on a flat account can open a position)");
					if (qty > Math.Abs(pos)) return Err("stop refused: qty " + qty + " > position " + Math.Abs(pos));
					if (RestingExits(acct, inst, pos).Count > 0) return Err("stop refused: a resting exit already exists; use change_stop (one resting exit only)");
					return Submit(acct, inst, pos > 0 ? OrderAction.Sell : OrderAction.Buy, OrderType.StopMarket, qty, px, name);
				}
				case "change_stop":
				{
					Need(p, 5, "change_stop <account> <name> <qty> <stopPrice>");
					Account acct = AllowedAccount(p[1]);
					Order o = FindOrder(acct, p[2]);
					if (o == null) return Err("unknown order " + p[2]);
					if (Order.IsTerminalState(o.OrderState)) return Err("order " + p[2] + " is " + o.OrderState);
					if (o.OrderType != OrderType.StopMarket) return Err("order " + p[2] + " is not a stop");
					int qty = Qty(p[3]);
					double px = Price(p[4]);
					int pos = SignedPosition(acct, o.Instrument);
					if (qty > Math.Abs(pos)) return Err("change refused: qty " + qty + " > position " + Math.Abs(pos));
					o.QuantityChanged = qty;
					o.StopPriceChanged = px;
					acct.Change(new[] { o });
					return Ok("change_stop", "\"name\":" + Q(o.Name) + ",\"state\":" + Q(o.OrderState.ToString()));
				}
				case "exit":
				{
					Need(p, 5, "exit <account> <instrument> <qty> <name>");
					Account acct = AllowedAccount(p[1]);
					Instrument inst = AllowedInstrument(p[2]);
					int qty = Qty(p[3]);
					string name = NewName(acct, p[4]);
					int pos = SignedPosition(acct, inst);
					if (pos == 0) return Err("exit refused: flat");
					if (qty > Math.Abs(pos)) return Err("exit refused: qty " + qty + " > position " + Math.Abs(pos));
					int stopQty = RestingExits(acct, inst, pos).Sum(o => o.Quantity);
					if (stopQty + qty > Math.Abs(pos))
						return Err("exit refused: resting stop " + stopQty + " + exit " + qty + " > position " + Math.Abs(pos) + " (shrink the stop first)");
					return Submit(acct, inst, pos > 0 ? OrderAction.Sell : OrderAction.Buy, OrderType.Market, qty, 0, name);
				}
				case "flatten":
				{
					Need(p, 3, "flatten <account> <instrument>");
					Account acct = AllowedAccount(p[1]);
					Instrument inst = AllowedInstrument(p[2]);
					Hook(acct);
					acct.Flatten(new[] { inst });
					Event("guard", "\"account\":" + Q(acct.Name) + ",\"instrument\":" + Q(inst.FullName) + ",\"action\":\"flatten\",\"reason\":\"requested\"");
					return Ok("flatten", "\"account\":" + Q(acct.Name) + ",\"instrument\":" + Q(inst.FullName));
				}
				case "cancel":
				{
					Need(p, 3, "cancel <account> <name>");
					Account acct = AllowedAccount(p[1]);
					Order o = FindOrder(acct, p[2]);
					if (o == null) return Err("unknown order " + p[2]);
					if (Order.IsTerminalState(o.OrderState)) return Err("order " + p[2] + " is already " + o.OrderState + "; nothing to cancel");
					acct.Cancel(new[] { o });
					return Ok("cancel", "\"name\":" + Q(o.Name) + ",\"state\":" + Q(o.OrderState.ToString()));
				}
				case "block":
				{
					Need(p, 3, "block <account> <reason...>");
					Account acct = AllowedAccount(p[1]);
					lock (sync) blocks[acct.Name] = string.Join(" ", p.Skip(2));
					return Ok("block", "\"account\":" + Q(acct.Name) + ",\"blocked\":" + Q(BlockReason(acct)));
				}
				case "unblock":
				{
					Account acct = AllowedAccount(Arg(p, 1));
					lock (sync) blocks.Remove(acct.Name);
					return Ok("unblock", "\"account\":" + Q(acct.Name) + ",\"blocked\":" + Q(BlockReason(acct)));
				}
			}
			return Err("unknown command " + cmd);
		}

		string Submit(Account acct, Instrument inst, OrderAction action, OrderType type, int qty, double stopPx, string name)
		{
			Hook(acct);
			Order o = acct.CreateOrder(inst, action, type, OrderEntry.Automated, TimeInForce.Day, qty, 0, stopPx, string.Empty, name, Core.Globals.MaxDate, null);
			acct.Submit(new[] { o });
			return Ok("submitted", "\"name\":" + Q(name) + ",\"action\":" + Q(action.ToString()) + ",\"type\":" + Q(type.ToString())
				+ ",\"qty\":" + qty + ",\"stop\":" + N(stopPx) + ",\"state\":" + Q(o.OrderState.ToString()));
		}

		// ---------------------------------------------------------------- refusals and limits
		string EntryRefusal(Account acct, Instrument inst)
		{
			if (acct.ConnectionStatus != ConnectionStatus.Connected) return "account " + acct.Name + " is " + acct.ConnectionStatus;
			string b = BlockReason(acct);
			if (b != null) return "blocked: " + b;
			DateTime t;
			lock (lastTickUtc)
				if (!lastTickUtc.TryGetValue(inst.FullName, out t)) return "no price feed for " + inst.FullName + " (subscribe first)";
			double age = (DateTime.UtcNow - t).TotalMilliseconds;
			if (age > feedStaleMs) return "price feed stale: " + (long)age + "ms > " + feedStaleMs + "ms";
			return null;
		}

		string BlockReason(Account acct)
		{
			lock (sync)
			{
				string a, b;
				blocks.TryGetValue(acct.Name, out a);
				lossBlocks.TryGetValue(acct.Name, out b);
				if (a != null && b != null) return a + "; " + b;
				return a ?? b;
			}
		}

		void CheckDailyLoss(Account acct)
		{
			string day = SessionDay();
			lock (sync)
			{
				string blockedDay;
				if (lossBlockDay.TryGetValue(acct.Name, out blockedDay) && blockedDay != day)
				{
					lossBlocks.Remove(acct.Name);
					lossBlockDay.Remove(acct.Name);
					Report("PTBBridge daily loss block on " + acct.Name + " cleared at session rollover (" + blockedDay + " -> " + day + ")", LogLevel.Information);
				}
			}
			if (dailyLossLimit <= 0) return;
			// NT8's RealizedProfitLoss: assumed to reset each session — NOT yet verified (watch status.realized across 17:00 CT).
			double pnl = acct.Get(AccountItem.RealizedProfitLoss, Currency.UsDollar) + acct.Get(AccountItem.UnrealizedProfitLoss, Currency.UsDollar);
			if (pnl > -dailyLossLimit) return;
			bool first;
			lock (sync)
			{
				first = !lossBlocks.ContainsKey(acct.Name);
				if (first) { lossBlocks[acct.Name] = "daily loss " + N(pnl) + " <= -" + N(dailyLossLimit); lossBlockDay[acct.Name] = day; }
			}
			if (!first) return;
			List<Instrument> insts;
			lock (acct.Positions) insts = acct.Positions.Where(x => x.MarketPosition != MarketPosition.Flat).Select(x => x.Instrument).ToList();
			if (insts.Count > 0) acct.Flatten(insts);
			Event("guard", "\"account\":" + Q(acct.Name) + ",\"action\":\"daily_loss_flatten\",\"pnl\":" + N(pnl) + ",\"limit\":" + N(dailyLossLimit));
			Report("PTBBridge DAILY LOSS LIMIT hit on " + acct.Name + ": pnl " + N(pnl) + " — flattened, entries blocked", LogLevel.Alert);
		}

		// ---------------------------------------------------------------- the guard (runs every second and on every fill)
		void GuardAll(string why)
		{
			if (!running) return;
			foreach (string name in allowedAccounts)
			{
				Account acct;
				lock (Account.All) acct = Account.All.FirstOrDefault(a => a.Name == name);
				if (acct == null || acct.ConnectionStatus != ConnectionStatus.Connected) continue;
				try { Guard(acct, why); CheckDailyLoss(acct); }
				catch (Exception ex) { Report("PTBBridge guard error on " + name + ": " + ex.Message, LogLevel.Warning); }
			}
		}

		void Guard(Account acct, string why)
		{
			List<Order> working;
			lock (acct.Orders)
				working = acct.Orders.Where(o => IsPtb(o) && !Order.IsTerminalState(o.OrderState) && o.OrderType != OrderType.Market).ToList();
			foreach (var g in working.GroupBy(o => o.Instrument))
			{
				Instrument inst = g.Key;
				int pos = SignedPosition(acct, inst);
				foreach (Order o in g)
				{
					if (InFlight(o)) continue;
					bool closing = pos != 0 && IsSell(o.OrderAction) == (pos > 0);
					if (pos == 0 || !closing)
						GuardCancel(acct, o, pos == 0 ? "resting order while flat" : "resting order on the opening side", why);
				}
				if (pos == 0) continue;
				List<Order> exits = g.Where(o => !InFlight(o) && IsSell(o.OrderAction) == (pos > 0)).OrderBy(o => o.Time).ToList();
				foreach (Order extra in exits.Skip(1)) GuardCancel(acct, extra, "more than one resting exit", why);
				if (exits.Count > 0 && exits[0].Quantity > Math.Abs(pos))
				{
					Order s = exits[0];
					int from = s.Quantity;
					s.QuantityChanged = Math.Abs(pos);
					acct.Change(new[] { s });
					Event("guard", "\"account\":" + Q(acct.Name) + ",\"name\":" + Q(s.Name) + ",\"action\":\"reduce\",\"from\":" + from
						+ ",\"to\":" + Math.Abs(pos) + ",\"reason\":\"resting exit larger than position\",\"trigger\":" + Q(why));
				}
			}
			// report, never touch, resting orders not placed by PTB
			List<Order> foreign;
			lock (acct.Orders) foreign = acct.Orders.Where(o => !IsPtb(o) && !Order.IsTerminalState(o.OrderState)).ToList();
			if (foreign.Count > 0 && why == "reconcile")
				Event("foreign_orders", "\"account\":" + Q(acct.Name) + ",\"count\":" + foreign.Count);
		}

		void GuardCancel(Account acct, Order o, string reason, string why)
		{
			acct.Cancel(new[] { o });
			Event("guard", "\"account\":" + Q(acct.Name) + ",\"name\":" + Q(o.Name) + ",\"action\":\"cancel\",\"reason\":" + Q(reason) + ",\"trigger\":" + Q(why));
		}

		static bool InFlight(Order o)
		{
			return o.OrderState == OrderState.ChangePending || o.OrderState == OrderState.ChangeSubmitted
				|| o.OrderState == OrderState.CancelPending || o.OrderState == OrderState.CancelSubmitted
				|| o.OrderState == OrderState.Submitted || o.OrderState == OrderState.Initialized;
		}

		static bool IsPtb(Order o) { return o.Name != null && o.Name.StartsWith("ptb-"); }
		static bool IsSell(OrderAction a) { return a == OrderAction.Sell || a == OrderAction.SellShort; }

		List<Order> RestingExits(Account acct, Instrument inst, int pos)
		{
			lock (acct.Orders)
				return acct.Orders.Where(o => o.Instrument == inst && !Order.IsTerminalState(o.OrderState) && o.OrderType != OrderType.Market
					&& IsSell(o.OrderAction) == (pos > 0)).ToList();
		}

		static int SignedPosition(Account acct, Instrument inst)
		{
			lock (acct.Positions)
			{
				Position x = acct.Positions.FirstOrDefault(q => q.Instrument == inst);
				if (x == null || x.MarketPosition == MarketPosition.Flat) return 0;
				return x.MarketPosition == MarketPosition.Long ? x.Quantity : -x.Quantity;
			}
		}

		// ---------------------------------------------------------------- lookups and argument parsing
		Account AllowedAccount(string name)
		{
			if (!allowedAccounts.Contains(name)) throw new InvalidOperationException("account " + name + " is not in ptbbridge.cfg accounts=");
			Account acct;
			lock (Account.All) acct = Account.All.FirstOrDefault(a => a.Name == name);
			if (acct == null) throw new InvalidOperationException("account " + name + " not present in NT8");
			Hook(acct);
			return acct;
		}

		Instrument AllowedInstrument(string arg)
		{
			string n = arg.Replace('_', ' ');
			Instrument inst = Instrument.GetInstrument(n);
			if (inst == null) throw new InvalidOperationException("unknown instrument " + n);
			if (!allowedRoots.Contains(inst.MasterInstrument.Name.ToUpperInvariant()))
				throw new InvalidOperationException("instrument " + inst.FullName + " is not in ptbbridge.cfg instruments=");
			return inst;
		}

		static Order FindOrder(Account acct, string name)
		{
			lock (acct.Orders) return acct.Orders.LastOrDefault(o => o.Name == name);
		}

		string NewName(Account acct, string name)
		{
			if (!NameRx.IsMatch(name)) throw new InvalidOperationException("order name must match ptb-[A-Za-z0-9-]{1,48}");
			if (FindOrder(acct, name) != null) throw new InvalidOperationException("order name " + name + " already used on " + acct.Name);
			return name;
		}

		int Qty(string s)
		{
			int q = int.Parse(s, CultureInfo.InvariantCulture);
			if (q < 1 || q > maxOrderQty) throw new InvalidOperationException("qty must be 1.." + maxOrderQty + " (max_order_qty)");
			return q;
		}

		static double Price(string s)
		{
			double d = double.Parse(s, CultureInfo.InvariantCulture);
			if (d <= 0) throw new InvalidOperationException("price must be > 0");
			return d;
		}

		static OrderAction Side(string s)
		{
			string u = s.ToUpperInvariant();
			if (u == "BUY") return OrderAction.Buy;
			if (u == "SELL") return OrderAction.Sell;
			throw new InvalidOperationException("side must be BUY or SELL");
		}

		static void Need(string[] p, int n, string usage) { if (p.Length < n) throw new InvalidOperationException("usage: " + usage); }
		static string Arg(string[] p, int i) { return p.Length > i ? p[i] : ""; }

		// ---------------------------------------------------------------- events
		void Hook(Account acct)
		{
			lock (hooked)
				if (hooked.Add(acct)) { acct.OrderUpdate += OnOrderUpdate; acct.ExecutionUpdate += OnExecutionUpdate; acct.PositionUpdate += OnPositionUpdate; }
		}

		void OnOrderUpdate(object sender, OrderEventArgs e)
		{
			lastBrokerEventUtc = DateTime.UtcNow;
			Order o = e.Order;
			Event("order", "\"account\":" + Q(o.Account.Name) + ",\"name\":" + Q(o.Name) + ",\"id\":" + Q(e.OrderId)
				+ ",\"instrument\":" + Q(o.Instrument.FullName) + ",\"action\":" + Q(o.OrderAction.ToString()) + ",\"type\":" + Q(o.OrderType.ToString())
				+ ",\"qty\":" + e.Quantity + ",\"stop\":" + N(e.StopPrice) + ",\"state\":" + Q(e.OrderState.ToString())
				+ ",\"filled\":" + e.Filled + ",\"avg\":" + N(e.AverageFillPrice) + ",\"error\":" + Q(e.Error.ToString())
				+ ",\"comment\":" + Q(e.Comment) + ",\"broker_time\":" + Q(Utc(e.Time)));
		}

		void OnExecutionUpdate(object sender, ExecutionEventArgs e)
		{
			lastBrokerEventUtc = DateTime.UtcNow;
			Order o = e.Execution.Order;
			Event("fill", "\"account\":" + Q(e.Execution.Account.Name) + ",\"name\":" + Q(o != null ? o.Name : "") + ",\"exec_id\":" + Q(e.ExecutionId)
				+ ",\"instrument\":" + Q(e.Execution.Instrument.FullName) + ",\"side\":" + Q(o != null ? o.OrderAction.ToString() : e.MarketPosition.ToString())
				+ ",\"price\":" + N(e.Price) + ",\"qty\":" + e.Quantity + ",\"broker_time\":" + Q(Utc(e.Time)));
			ThreadPool.QueueUserWorkItem(_ => { try { Guard(e.Execution.Account, "fill"); } catch { } });
		}

		// The fill event can arrive before NT8 updates the position (P1 test: the fill pass missed an orphan stop that the
		// 1 s timer then caught). Guard again once the position itself has changed.
		void OnPositionUpdate(object sender, PositionEventArgs e)
		{
			lastBrokerEventUtc = DateTime.UtcNow;
			Account acct = e.Position.Account;
			Event("position", "\"account\":" + Q(acct.Name) + ",\"instrument\":" + Q(e.Position.Instrument.FullName)
				+ ",\"side\":" + Q(e.MarketPosition.ToString()) + ",\"qty\":" + e.Quantity + ",\"avg\":" + N(e.AveragePrice));
			ThreadPool.QueueUserWorkItem(_ => { try { Guard(acct, "position"); } catch { } });
		}

		void OnMarketData(object sender, MarketDataEventArgs e)
		{
			if (e.MarketDataType != MarketDataType.Last && e.MarketDataType != MarketDataType.Bid && e.MarketDataType != MarketDataType.Ask) return;
			string inst = e.Instrument.FullName;
			lock (lastTickUtc) lastTickUtc[inst] = DateTime.UtcNow;
			if (e.MarketDataType == MarketDataType.Last) lock (lastPrice) lastPrice[inst] = e.Price;
			Event("tick", "\"instrument\":" + Q(inst) + ",\"type\":" + Q(e.MarketDataType.ToString()) + ",\"price\":" + N(e.Price)
				+ ",\"volume\":" + e.Volume + ",\"exchange_time\":" + Q(Utc(e.Time)));
		}

		// ---------------------------------------------------------------- status (counts, not booleans)
		string StatusJson()
		{
			var sb = new StringBuilder("{\"accounts\":[");
			int connected = 0, i = 0;
			foreach (string name in allowedAccounts)
			{
				Account acct;
				lock (Account.All) acct = Account.All.FirstOrDefault(a => a.Name == name);
				if (i++ > 0) sb.Append(',');
				if (acct == null) { sb.Append("{\"name\":" + Q(name) + ",\"present\":false}"); continue; }
				if (acct.ConnectionStatus == ConnectionStatus.Connected) connected++;
				int working, ptbWorking, positions;
				lock (acct.Orders)
				{
					working = acct.Orders.Count(o => !Order.IsTerminalState(o.OrderState));
					ptbWorking = acct.Orders.Count(o => IsPtb(o) && !Order.IsTerminalState(o.OrderState));
				}
				lock (acct.Positions) positions = acct.Positions.Count(x => x.MarketPosition != MarketPosition.Flat);
				sb.Append("{\"name\":" + Q(name) + ",\"present\":true,\"connection\":" + Q(acct.ConnectionStatus.ToString())
					+ ",\"open_positions\":" + positions + ",\"working_orders\":" + working + ",\"ptb_working_orders\":" + ptbWorking
					+ ",\"realized\":" + N(acct.Get(AccountItem.RealizedProfitLoss, Currency.UsDollar))
					+ ",\"unrealized\":" + N(acct.Get(AccountItem.UnrealizedProfitLoss, Currency.UsDollar))
					+ ",\"blocked\":" + Q(BlockReason(acct)) + "}");
			}
			sb.Append("],\"accounts_allowed\":" + allowedAccounts.Count + ",\"accounts_connected\":" + connected + ",\"feeds\":[");
			i = 0;
			lock (lastTickUtc)
				foreach (var kv in lastTickUtc)
				{
					if (i++ > 0) sb.Append(',');
					double last;
					lock (lastPrice) lastPrice.TryGetValue(kv.Key, out last);
					sb.Append("{\"instrument\":" + Q(kv.Key) + ",\"age_ms\":" + (long)(DateTime.UtcNow - kv.Value).TotalMilliseconds + ",\"last\":" + N(last) + "}");
				}
			int nClients;
			lock (clients) nClients = clients.Count(c => c.Authed);
			sb.Append("],\"feeds_subscribed\":" + feeds.Count + ",\"clients\":" + nClients
				+ ",\"last_broker_event_age_ms\":" + AgeMs(lastBrokerEventUtc) + ",\"last_ping_age_ms\":" + AgeMs(lastPingUtc)
				+ ",\"limits\":" + LimitsJson() + ",\"session_day\":" + Q(SessionDay())
				+ ",\"utc\":" + Q(DateTime.UtcNow.ToString("o")) + "}");
			return sb.ToString();
		}

		string LimitsJson()
		{
			return "{\"max_order_qty\":" + maxOrderQty + ",\"max_position_qty\":" + maxPositionQty + ",\"daily_loss_limit\":" + N(dailyLossLimit)
				+ ",\"feed_stale_ms\":" + feedStaleMs + ",\"roots\":" + Q(string.Join(",", allowedRoots)) + "}";
		}

		static long AgeMs(DateTime t) { return t == DateTime.MinValue ? -1 : (long)(DateTime.UtcNow - t).TotalMilliseconds; }

		// CME session day: the session that opens at 17:00 Central belongs to the next calendar day.
		static readonly TimeZoneInfo Central = TimeZoneInfo.FindSystemTimeZoneById("Central Standard Time");
		static string SessionDay()
		{
			DateTime ct = TimeZoneInfo.ConvertTimeFromUtc(DateTime.UtcNow, Central);
			return (ct.Hour >= 17 ? ct.Date.AddDays(1) : ct.Date).ToString("yyyy-MM-dd");
		}

		string PositionsJson(Account acct)
		{
			lock (acct.Positions)
				return "[" + string.Join(",", acct.Positions.Where(x => x.MarketPosition != MarketPosition.Flat).Select(x =>
					"{\"instrument\":" + Q(x.Instrument.FullName) + ",\"side\":" + Q(x.MarketPosition.ToString())
					+ ",\"qty\":" + x.Quantity + ",\"avg\":" + N(x.AveragePrice) + "}")) + "]";
		}

		string OrdersJson(Account acct, bool workingOnly)
		{
			lock (acct.Orders)
				return "[" + string.Join(",", acct.Orders.Where(o => !workingOnly || !Order.IsTerminalState(o.OrderState)).Select(o =>
					"{\"name\":" + Q(o.Name) + ",\"id\":" + Q(o.OrderId) + ",\"ptb\":" + (IsPtb(o) ? "true" : "false")
					+ ",\"instrument\":" + Q(o.Instrument.FullName) + ",\"action\":" + Q(o.OrderAction.ToString())
					+ ",\"type\":" + Q(o.OrderType.ToString()) + ",\"qty\":" + o.Quantity + ",\"stop\":" + N(o.StopPrice)
					+ ",\"limit\":" + N(o.LimitPrice) + ",\"filled\":" + o.Filled + ",\"avg\":" + N(o.AverageFillPrice)
					+ ",\"state\":" + Q(o.OrderState.ToString()) + ",\"time\":" + Q(Utc(o.Time)) + "}")) + "]";
		}

		// ---------------------------------------------------------------- plumbing
		void Event(string ev, string body)
		{
			string json = "{\"ev\":" + Q(ev) + ",\"ts\":" + Q(DateTime.UtcNow.ToString("o")) + "," + body + "}";
			List<Client> snapshot;
			lock (clients) snapshot = clients.Where(c => c.Authed).ToList();
			foreach (Client c in snapshot) Send(c, json);
			if (ev == "guard") Report("PTBBridge " + json, LogLevel.Warning);
		}

		static void Send(Client c, string json) { try { lock (c) c.Writer.WriteLine(json); } catch { } }

		void Report(string msg, LogLevel level) { Print(msg); Log(msg, level); }

		// NT8 times are PC-local (Kind Unspecified); the VM runs on Pacific time. Always emit UTC.
		static string Utc(DateTime t)
		{
			if (t == DateTime.MinValue) return "";
			DateTime u = t.Kind == DateTimeKind.Utc ? t : TimeZoneInfo.ConvertTimeToUtc(DateTime.SpecifyKind(t, DateTimeKind.Unspecified), TimeZoneInfo.Local);
			return u.ToString("o");
		}

		static string Ok(string cmd, string body) { return "{\"ok\":true,\"cmd\":" + Q(cmd) + (body.Length > 0 ? "," + body : "") + "}"; }
		static string Err(string msg) { return "{\"ok\":false,\"error\":" + Q(msg) + "}"; }
		static string N(double d) { return d.ToString("R", CultureInfo.InvariantCulture); }

		static string Q(string s)
		{
			if (s == null) return "null";
			var sb = new StringBuilder("\"");
			foreach (char ch in s)
			{
				if (ch == '"' || ch == '\\') sb.Append('\\').Append(ch);
				else if (ch < 0x20) sb.AppendFormat("\\u{0:x4}", (int)ch);
				else sb.Append(ch);
			}
			return sb.Append('"').ToString();
		}
	}
}
