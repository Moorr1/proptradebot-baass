#!/usr/bin/env python3
"""
PropTradeBot — Standalone Mac App with GUI
Double-click to run. No terminal needed.
"""

import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext, filedialog
import os
import sys
import json
import subprocess
import threading
import time
from datetime import datetime

# Add bot directory to path
BOT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BOT_DIR)

class PropTradeBotApp:
    def __init__(self, root):
        self.root = root
        self.root.title("PropTradeBot")
        self.root.geometry("600x700")
        self.root.configure(bg="#0f172a")
        
        # Try to set app icon
        try:
            self.root.iconphoto(False, tk.PhotoImage(file=os.path.join(BOT_DIR, "icon.png")))
        except:
            pass
        
        self.bot_process = None
        self.bot_thread = None
        self.running = False
        
        self.setup_ui()
        self.load_config()
        self.check_api_key()
    
    def setup_ui(self):
        # Header
        header = tk.Frame(self.root, bg="#0f172a", padx=20, pady=20)
        header.pack(fill="x")
        
        tk.Label(header, text="🤖 PropTradeBot", font=("Helvetica", 24, "bold"), 
                fg="#38bdf8", bg="#0f172a").pack(anchor="w")
        tk.Label(header, text="Automated Futures Trading", font=("Helvetica", 12),
                fg="#94a3b8", bg="#0f172a").pack(anchor="w")
        
        # Status Card
        status_card = tk.Frame(self.root, bg="#1e293b", padx=20, pady=16)
        status_card.pack(fill="x", padx=20, pady=(0, 10))
        
        tk.Label(status_card, text="Status", font=("Helvetica", 14, "bold"),
                fg="#f1f5f9", bg="#1e293b").pack(anchor="w")
        
        self.status_label = tk.Label(status_card, text="● Stopped", font=("Helvetica", 16, "bold"),
                                     fg="#ef4444", bg="#1e293b")
        self.status_label.pack(anchor="w", pady=(8, 0))
        
        self.status_detail = tk.Label(status_card, text="Click Start to begin trading",
                                     font=("Helvetica", 11), fg="#94a3b8", bg="#1e293b")
        self.status_detail.pack(anchor="w")
        
        # API Key Card
        api_card = tk.Frame(self.root, bg="#1e293b", padx=20, pady=16)
        api_card.pack(fill="x", padx=20, pady=(0, 10))
        
        tk.Label(api_card, text="API Key", font=("Helvetica", 14, "bold"),
                fg="#f1f5f9", bg="#1e293b").pack(anchor="w")
        
        api_input_frame = tk.Frame(api_card, bg="#1e293b")
        api_input_frame.pack(fill="x", pady=(8, 0))
        
        self.api_key_var = tk.StringVar()
        self.api_key_entry = tk.Entry(api_input_frame, textvariable=self.api_key_var,
                                      font=("Courier", 11), show="•",
                                      bg="#0f172a", fg="#e2e8f0",
                                      insertbackground="#e2e8f0",
                                      relief="flat", highlightthickness=1,
                                      highlightcolor="#38bdf8",
                                      highlightbackground="#334155")
        self.api_key_entry.pack(side="left", fill="x", expand=True, ipady=6)
        
        self.show_key_btn = tk.Button(api_input_frame, text="Show", command=self.toggle_key_visibility,
                                      bg="#334155", fg="#e2e8f0", relief="flat",
                                      padx=12, cursor="hand2")
        self.show_key_btn.pack(side="right", padx=(8, 0))
        
        tk.Label(api_card, text="Get your API key from the PropTradeBot dashboard",
                font=("Helvetica", 10), fg="#64748b", bg="#1e293b").pack(anchor="w", pady=(4, 0))
        
        # Config Card
        config_card = tk.Frame(self.root, bg="#1e293b", padx=20, pady=16)
        config_card.pack(fill="x", padx=20, pady=(0, 10))
        
        tk.Label(config_card, text="Trading Accounts", font=("Helvetica", 14, "bold"),
                fg="#f1f5f9", bg="#1e293b").pack(anchor="w")
        
        self.config_text = scrolledtext.ScrolledText(config_card, height=8,
                                                      font=("Courier", 10),
                                                      bg="#0f172a", fg="#e2e8f0",
                                                      insertbackground="#e2e8f0",
                                                      relief="flat", highlightthickness=1,
                                                      highlightcolor="#38bdf8",
                                                      highlightbackground="#334155")
        self.config_text.pack(fill="x", pady=(8, 0))
        
        btn_frame = tk.Frame(config_card, bg="#1e293b")
        btn_frame.pack(fill="x", pady=(8, 0))
        
        tk.Button(btn_frame, text="Load Config File", command=self.load_config_file,
                 bg="#334155", fg="#e2e8f0", relief="flat", padx=16, pady=6,
                 cursor="hand2").pack(side="left", padx=(0, 8))
        
        tk.Button(btn_frame, text="Save Config", command=self.save_config,
                 bg="#334155", fg="#e2e8f0", relief="flat", padx=16, pady=6,
                 cursor="hand2").pack(side="left")
        
        # Control Buttons
        control_frame = tk.Frame(self.root, bg="#0f172a", padx=20, pady=10)
        control_frame.pack(fill="x")
        
        self.start_btn = tk.Button(control_frame, text="▶  Start Bot", command=self.start_bot,
                                   bg="#22c55e", fg="white", font=("Helvetica", 14, "bold"),
                                   relief="flat", padx=30, pady=12, cursor="hand2")
        self.start_btn.pack(side="left", fill="x", expand=True, padx=(0, 8))
        
        self.stop_btn = tk.Button(control_frame, text="⏹  Stop", command=self.stop_bot,
                                  bg="#dc2626", fg="white", font=("Helvetica", 14, "bold"),
                                  relief="flat", padx=30, pady=12, cursor="hand2",
                                  state="disabled")
        self.stop_btn.pack(side="right", fill="x", expand=True)
        
        # Log Output
        log_card = tk.Frame(self.root, bg="#1e293b", padx=20, pady=16)
        log_card.pack(fill="both", expand=True, padx=20, pady=(0, 20))
        
        tk.Label(log_card, text="Logs", font=("Helvetica", 14, "bold"),
                fg="#f1f5f9", bg="#1e293b").pack(anchor="w")
        
        self.log_text = scrolledtext.ScrolledText(log_card, height=10,
                                                   font=("Courier", 9),
                                                   bg="#0f172a", fg="#94a3b8",
                                                   relief="flat", state="disabled")
        self.log_text.pack(fill="both", expand=True, pady=(8, 0))
    
    def toggle_key_visibility(self):
        if self.api_key_entry.cget("show") == "•":
            self.api_key_entry.config(show="")
            self.show_key_btn.config(text="Hide")
        else:
            self.api_key_entry.config(show="•")
            self.show_key_btn.config(text="Show")
    
    def log(self, message):
        self.log_text.config(state="normal")
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.insert("end", f"[{timestamp}] {message}\n")
        self.log_text.see("end")
        self.log_text.config(state="disabled")
    
    def check_api_key(self):
        api_key = os.environ.get("PTB_API_KEY", "")
        if api_key:
            self.api_key_var.set(api_key)
            self.log("API key loaded from environment")
    
    def load_config(self):
        config_path = os.path.join(BOT_DIR, "config.json")
        if os.path.exists(config_path):
            try:
                with open(config_path, "r") as f:
                    config = json.load(f)
                self.config_text.delete("1.0", "end")
                self.config_text.insert("1.0", json.dumps(config, indent=2))
                self.log("Config loaded from config.json")
            except Exception as e:
                self.log(f"Error loading config: {e}")
                self.load_default_config()
        else:
            self.load_default_config()
    
    def load_default_config(self):
        default = {
            "accounts": [
                {
                    "name": "Topstep Account 1",
                    "broker": "topstep",
                    "account_id": "YOUR_ACCOUNT_ID",
                    "api_key": "YOUR_API_KEY",
                    "enabled": True
                }
            ],
            "strategy": {
                "contract": "MNQ",
                "contracts_per_entry": 5,
                "t1_target": 20,
                "t1_contracts": 3,
                "t2_target": 40,
                "t2_contracts": 1,
                "runner_target": 60,
                "runner_contracts": 1,
                "stop_loss": 35
            },
            "cloud": {
                "enabled": True,
                "api_url": "https://proptradebot.com"
            }
        }
        self.config_text.delete("1.0", "end")
        self.config_text.insert("1.0", json.dumps(default, indent=2))
        self.log("Loaded default config template")
    
    def load_config_file(self):
        path = filedialog.askopenfilename(filetypes=[("JSON files", "*.json")])
        if path:
            try:
                with open(path, "r") as f:
                    config = json.load(f)
                self.config_text.delete("1.0", "end")
                self.config_text.insert("1.0", json.dumps(config, indent=2))
                self.log(f"Config loaded from {path}")
            except Exception as e:
                messagebox.showerror("Error", f"Failed to load config: {e}")
    
    def save_config(self):
        try:
            config = json.loads(self.config_text.get("1.0", "end"))
            config_path = os.path.join(BOT_DIR, "config.json")
            with open(config_path, "w") as f:
                json.dump(config, f, indent=2)
            self.log("Config saved to config.json")
            messagebox.showinfo("Saved", "Configuration saved successfully!")
        except json.JSONDecodeError as e:
            messagebox.showerror("Invalid JSON", f"Config is not valid JSON:\n{e}")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to save config: {e}")
    
    def start_bot(self):
        api_key = self.api_key_var.get().strip()
        if not api_key:
            messagebox.showwarning("API Key Required", 
                "Please enter your API key from the PropTradeBot dashboard.")
            return
        
        # Save API key to environment for this session
        os.environ["PTB_API_KEY"] = api_key
        
        # Save config first
        try:
            config = json.loads(self.config_text.get("1.0", "end"))
            config_path = os.path.join(BOT_DIR, "config.json")
            with open(config_path, "w") as f:
                json.dump(config, f, indent=2)
        except Exception as e:
            messagebox.showerror("Config Error", f"Invalid config: {e}")
            return
        
        self.running = True
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.status_label.config(text="● Running", fg="#22c55e")
        self.status_detail.config(text="Bot is active and monitoring for signals")
        self.log("Starting PropTradeBot...")
        
        # Start bot in background thread
        self.bot_thread = threading.Thread(target=self._run_bot, daemon=True)
        self.bot_thread.start()
    
    def _run_bot(self):
        """Run the bot process and capture output"""
        try:
            bot_script = os.path.join(BOT_DIR, "server_projectx_v2.py")
            
            if not os.path.exists(bot_script):
                self.root.after(0, lambda: self.log("ERROR: Bot script not found!"))
                self.root.after(0, self.stop_bot)
                return
            
            env = os.environ.copy()
            env["PTB_API_KEY"] = self.api_key_var.get().strip()
            
            self.bot_process = subprocess.Popen(
                [sys.executable, bot_script],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=env,
                cwd=BOT_DIR
            )
            
            # Read output line by line
            for line in self.bot_process.stdout:
                if not self.running:
                    break
                self.root.after(0, lambda l=line.strip(): self.log(l))
            
            # Process ended
            return_code = self.bot_process.wait()
            if return_code != 0 and self.running:
                self.root.after(0, lambda: self.log(f"Bot exited with code {return_code}"))
            
        except Exception as e:
            self.root.after(0, lambda: self.log(f"ERROR: {e}"))
        finally:
            self.root.after(0, self._bot_stopped)
    
    def _bot_stopped(self):
        if self.running:
            self.running = False
            self.start_btn.config(state="normal")
            self.stop_btn.config(state="disabled")
            self.status_label.config(text="● Stopped", fg="#ef4444")
            self.status_detail.config(text="Click Start to begin trading")
            self.log("Bot stopped")
    
    def stop_bot(self):
        self.running = False
        self.log("Stopping bot...")
        
        if self.bot_process:
            try:
                self.bot_process.terminate()
                # Give it 5 seconds to shut down gracefully
                try:
                    self.bot_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.bot_process.kill()
                    self.bot_process.wait()
            except Exception as e:
                self.log(f"Error stopping bot: {e}")
        
        self._bot_stopped()
    
    def on_closing(self):
        if self.running:
            if messagebox.askyesno("Quit?", "Bot is running. Stop and quit?"):
                self.stop_bot()
                self.root.destroy()
        else:
            self.root.destroy()


def main():
    root = tk.Tk()
    app = PropTradeBotApp(root)
    root.protocol("WM_DELETE_WINDOW", app.on_closing)
    root.mainloop()


if __name__ == "__main__":
    main()
