/* WebSocket client — receives realtime snapshots + job updates */
const WS = {
  socket: null,
  retry: 0,
  handlers: [],

  connect() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const url = `${proto}://${location.host}/ws`;
    try {
      this.socket = new WebSocket(url);
    } catch (e) {
      return;
    }
    this.socket.onopen = () => { this.retry = 0; };
    this.socket.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      this.handlers.forEach((h) => {
        try { h(msg); } catch (e) { console.error(e); }
      });
    };
    this.socket.onclose = () => {
      const delay = Math.min(30000, 1000 * Math.pow(2, this.retry++));
      setTimeout(() => this.connect(), delay);
    };
    this.socket.onerror = () => { if (this.socket) this.socket.close(); };
  },

  on(fn) { this.handlers.push(fn); },
};

WS.connect();
