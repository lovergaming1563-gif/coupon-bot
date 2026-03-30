export default function App() {
  return (
    <div style={{
      minHeight: "100vh",
      background: "linear-gradient(135deg, #0f172a 0%, #1e293b 100%)",
      display: "flex",
      alignItems: "center",
      justifyContent: "center",
      fontFamily: "'Segoe UI', system-ui, sans-serif",
      color: "#f1f5f9",
    }}>
      <div style={{ textAlign: "center", padding: "2rem" }}>
        <div style={{ fontSize: "4rem", marginBottom: "1rem" }}>🤖</div>
        <h1 style={{
          fontSize: "2rem",
          fontWeight: 700,
          marginBottom: "0.5rem",
          background: "linear-gradient(90deg, #38bdf8, #818cf8)",
          WebkitBackgroundClip: "text",
          WebkitTextFillColor: "transparent",
        }}>
          Coupon Bot
        </h1>
        <div style={{
          display: "inline-flex",
          alignItems: "center",
          gap: "0.5rem",
          background: "rgba(34,197,94,0.15)",
          border: "1px solid rgba(34,197,94,0.3)",
          borderRadius: "999px",
          padding: "0.4rem 1rem",
          marginBottom: "1.5rem",
          fontSize: "0.9rem",
          color: "#4ade80",
        }}>
          <span style={{
            width: 8, height: 8, borderRadius: "50%",
            background: "#4ade80",
            display: "inline-block",
            animation: "pulse 2s infinite",
          }} />
          Online &amp; Running 24/7
        </div>
        <p style={{ color: "#94a3b8", marginBottom: "2rem", fontSize: "1rem" }}>
          Myntra Coupon Bot is active and ready to serve customers.
        </p>
        <div style={{
          display: "grid",
          gridTemplateColumns: "repeat(2, 1fr)",
          gap: "1rem",
          maxWidth: 400,
          margin: "0 auto 2rem",
        }}>
          {[
            { icon: "🟢", label: "₹100 Coupon", price: "₹35" },
            { icon: "🔵", label: "₹150 Coupon", price: "₹35" },
            { icon: "⚡", label: "Instant Delivery", price: "" },
            { icon: "🔒", label: "Secure Payments", price: "" },
          ].map((item, i) => (
            <div key={i} style={{
              background: "rgba(255,255,255,0.05)",
              border: "1px solid rgba(255,255,255,0.1)",
              borderRadius: "0.75rem",
              padding: "1rem",
            }}>
              <div style={{ fontSize: "1.5rem" }}>{item.icon}</div>
              <div style={{ fontSize: "0.85rem", color: "#cbd5e1", marginTop: "0.25rem" }}>{item.label}</div>
              {item.price && (
                <div style={{ fontSize: "1rem", fontWeight: 700, color: "#38bdf8" }}>{item.price}</div>
              )}
            </div>
          ))}
        </div>
        <a
          href="https://t.me/MyntraCouponsupport_bot"
          target="_blank"
          rel="noopener noreferrer"
          style={{
            display: "inline-block",
            background: "linear-gradient(90deg, #0ea5e9, #6366f1)",
            color: "#fff",
            textDecoration: "none",
            padding: "0.75rem 2rem",
            borderRadius: "999px",
            fontWeight: 600,
            fontSize: "1rem",
            transition: "opacity 0.2s",
          }}
          onMouseOver={e => (e.currentTarget.style.opacity = "0.85")}
          onMouseOut={e => (e.currentTarget.style.opacity = "1")}
        >
          Open in Telegram →
        </a>
        <style>{`
          @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.4; }
          }
        `}</style>
      </div>
    </div>
  );
}
