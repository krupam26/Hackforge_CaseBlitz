import { useEffect, useMemo, useRef, useState } from "react";
import {
  getChallengeStatus, goOffline, goOnlineAndIngest, recordOrderAccept, recordOrderOffer,
  recordPrecisionConsent, respondToChallenge, getQueueHealth, type QueueHealth,
} from "./telemetry";

type Tab = "home" | "orders" | "earnings" | "profile";
type Page =
  | Tab
  | "order"
  | "notifications"
  | "verification"
  | "support"
  | "settings";

type Order = {
  id: string;
  pickup: string;
  pickupAddress: string;
  dropoff: string;
  dropoffAddress: string;
  distance: string;
  duration: string;
  payout: number;
  instructions: string;
};

type Scenario = {
  id: string;
  label: string;
  message: string;
  verification: "none" | "requested" | "success" | "failed" | "review";
  network?: boolean;
};

const demoOrder: Order = {
  id: "TM-4827",
  pickup: "Fresh Basket",
  pickupAddress: "Link Road, Andheri West",
  dropoff: "Lokhandwala Complex",
  dropoffAddress: "Building 7, Shastri Nagar",
  distance: "4.2 km",
  duration: "24 min",
  payout: 86,
  instructions: "Leave at reception. Call only if access is restricted.",
};

const initialCompleted: Order[] = [
  {
    ...demoOrder,
    id: "TM-4798",
    pickup: "Daily Kitchen",
    dropoff: "Oshiwara",
    payout: 74,
  },
  {
    ...demoOrder,
    id: "TM-4771",
    pickup: "Green Grocer",
    dropoff: "Versova",
    payout: 92,
  },
  {
    ...demoOrder,
    id: "TM-4746",
    pickup: "Quick Mart",
    dropoff: "Four Bungalows",
    payout: 68,
  },
];

const scenarios: Scenario[] = [
  {
    id: "normal",
    label: "Normal session",
    message: "All services are available.",
    verification: "none",
  },
  {
    id: "device",
    label: "Legitimate device change",
    message: "Your current session is active.",
    verification: "none",
  },
  {
    id: "location",
    label: "Legitimate location change",
    message: "Your current delivery zone is available.",
    verification: "none",
  },
  {
    id: "variation",
    label: "Temporary behavioural variation",
    message: "Your delivery workflow continues normally.",
    verification: "none",
  },
  {
    id: "hours",
    label: "Unusual working hours",
    message: "Your shift is active.",
    verification: "none",
  },
  {
    id: "network",
    label: "Poor network or missing signals",
    message: "Some updates may be delayed.",
    verification: "none",
    network: true,
  },
  {
    id: "return",
    label: "Return after a long break",
    message: "Welcome back. You can go online when ready.",
    verification: "none",
  },
  {
    id: "handoff",
    label: "Possible behavioural handoff",
    message: "An account check is waiting.",
    verification: "requested",
  },
  {
    id: "requested",
    label: "Verification requested",
    message: "Complete an account check when safely stopped.",
    verification: "requested",
  },
  {
    id: "verified",
    label: "Verification completed",
    message: "Your account check is complete.",
    verification: "success",
  },
  {
    id: "failed",
    label: "Verification failed",
    message: "Your account check needs another attempt.",
    verification: "failed",
  },
  {
    id: "review",
    label: "Review pending",
    message: "Your account update is under review.",
    verification: "review",
  },
];

const stageCopy = [
  {
    label: "Delivery accepted",
    action: "Navigate to pickup",
    detail: "Head to Fresh Basket for pickup.",
  },
  {
    label: "Heading to pickup",
    action: "I've arrived",
    detail: "Pickup is approximately 8 minutes away.",
  },
  {
    label: "Arrived at pickup",
    action: "Confirm pickup",
    detail: "Collect the sealed order from the pickup counter.",
  },
  {
    label: "Order picked up",
    action: "Navigate to customer",
    detail: "Check the order number before leaving.",
  },
  {
    label: "Heading to drop-off",
    action: "I've arrived",
    detail: "The destination is approximately 16 minutes away.",
  },
  {
    label: "Arrived at destination",
    action: "Complete delivery",
    detail: "Follow the customer's delivery instructions.",
  },
];

const earningData = {
  Today: {
    total: 642,
    deliveries: 8,
    incentive: 120,
    bars: [22, 45, 34, 62, 48, 74, 58],
    labels: ["8a", "10a", "12p", "2p", "4p", "6p", "Now"],
  },
  "This week": {
    total: 4280,
    deliveries: 54,
    incentive: 620,
    bars: [45, 68, 52, 74, 88, 60, 38],
    labels: ["M", "T", "W", "T", "F", "S", "S"],
  },
  "This month": {
    total: 16840,
    deliveries: 214,
    incentive: 2400,
    bars: [48, 72, 58, 90],
    labels: ["W1", "W2", "W3", "W4"],
  },
};

const iconPaths: Record<string, React.ReactNode> = {
  home: (
    <>
      <path d="M3 10.8 12 3l9 7.8" />
      <path d="M5 9.8V21h14V9.8M9 21v-6h6v6" />
    </>
  ),
  orders: (
    <>
      <rect x="4" y="3" width="16" height="18" rx="2" />
      <path d="M8 8h8M8 12h8M8 16h5" />
    </>
  ),
  wallet: (
    <>
      <path d="M3 6h16a2 2 0 0 1 2 2v11H5a2 2 0 0 1-2-2V6Z" />
      <path d="M3 8V5a2 2 0 0 1 2-2h12v3M16 12h5v4h-5a2 2 0 0 1 0-4Z" />
    </>
  ),
  user: (
    <>
      <circle cx="12" cy="8" r="4" />
      <path d="M4.5 21a7.5 7.5 0 0 1 15 0" />
    </>
  ),
  bell: (
    <>
      <path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9" />
      <path d="M10 21h4" />
    </>
  ),
  pin: (
    <>
      <path d="M20 10c0 5-8 11-8 11S4 15 4 10a8 8 0 1 1 16 0Z" />
      <circle cx="12" cy="10" r="2.5" />
    </>
  ),
  arrow: <path d="m9 18 6-6-6-6" />,
  back: <path d="m15 18-6-6 6-6" />,
  clock: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 7v5l3 2" />
    </>
  ),
  route: (
    <>
      <circle cx="6" cy="18" r="2" />
      <circle cx="18" cy="6" r="2" />
      <path d="M8 18h3a3 3 0 0 0 0-6h2a3 3 0 0 0 3-3V8" />
    </>
  ),
  check: <path d="m5 12 4 4L19 6" />,
  close: (
    <>
      <path d="m6 6 12 12M18 6 6 18" />
    </>
  ),
  phone: (
    <>
      <rect x="6.5" y="2.5" width="11" height="19" rx="2" />
      <path d="M10 5h4M11 18.5h2" />
    </>
  ),
  help: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M9.5 9a2.7 2.7 0 1 1 4.1 2.3c-1 .6-1.6 1.1-1.6 2.2M12 17h.01" />
    </>
  ),
  shield: (
    <>
      <path d="M12 3 20 6v6c0 5-3.4 8-8 9-4.6-1-8-4-8-9V6l8-3Z" />
      <path d="m8.5 12 2.2 2.2 4.8-5" />
    </>
  ),
  alert: (
    <>
      <path d="M12 3 2.5 20h19Z" />
      <path d="M12 9v5M12 17h.01" />
    </>
  ),
  car: (
    <>
      <path d="m5 16-1-3 2-6h12l2 6-1 3" />
      <path d="M3 16h18v3H3zM6 19v2M18 19v2M7 12h10" />
    </>
  ),
  settings: (
    <>
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1-2.8 2.8-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.6v.2h-4V21a1.7 1.7 0 0 0-1-1.6 1.7 1.7 0 0 0-1.9.3l-.1.1L4.2 17l.1-.1a1.7 1.7 0 0 0 .3-1.9A1.7 1.7 0 0 0 3 14H2.8v-4H3a1.7 1.7 0 0 0 1.6-1 1.7 1.7 0 0 0-.3-1.9L4.2 7 7 4.2l.1.1a1.7 1.7 0 0 0 1.9.3A1.7 1.7 0 0 0 10 3v-.2h4V3a1.7 1.7 0 0 0 1 1.6 1.7 1.7 0 0 0 1.9-.3l.1-.1L19.8 7l-.1.1a1.7 1.7 0 0 0-.3 1.9 1.7 1.7 0 0 0 1.6 1h.2v4H21a1.7 1.7 0 0 0-1.6 1Z" />
    </>
  ),
  globe: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18" />
    </>
  ),
  lock: (
    <>
      <rect x="4" y="10" width="16" height="11" rx="2" />
      <path d="M8 10V7a4 4 0 0 1 8 0v3" />
    </>
  ),
  gift: (
    <>
      <rect x="3" y="9" width="18" height="12" rx="2" />
      <path d="M12 9v12M3 13h18M12 9H7.5a2.5 2.5 0 1 1 2.2-3.7L12 9Zm0 0h4.5a2.5 2.5 0 1 0-2.2-3.7L12 9Z" />
    </>
  ),
  spark: <path d="m12 3 1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8L12 3Z" />,
  chat: (
    <>
      <path d="M21 11.5a8 8 0 0 1-9 7.9L5 21l1.6-4A8 8 0 1 1 21 11.5Z" />
      <path d="M9 12h.01M12 12h.01M15 12h.01" />
    </>
  ),
  navigation: (
    <>
      <path d="m3 11 18-8-8 18-2-8-8-2Z" />
      <path d="m11 13 4-4" />
    </>
  ),
  more: (
    <>
      <circle cx="5" cy="12" r="1" />
      <circle cx="12" cy="12" r="1" />
      <circle cx="19" cy="12" r="1" />
    </>
  ),
};

function Icon({ name, size = 20 }: { name: string; size?: number }) {
  return (
    <svg
      className="icon"
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {iconPaths[name] ?? iconPaths.help}
    </svg>
  );
}

function Button({
  children,
  variant = "primary",
  icon,
  onClick,
  disabled,
}: {
  children: React.ReactNode;
  variant?: "primary" | "secondary" | "ghost" | "danger";
  icon?: string;
  onClick?: () => void;
  disabled?: boolean;
}) {
  return (
    <button
      className={`button ${variant}`}
      onClick={onClick}
      disabled={disabled}
    >
      {icon && <Icon name={icon} size={18} />}
      {children}
    </button>
  );
}

function Header({
  onHome,
  onNotifications,
  unread,
}: {
  onHome: () => void;
  onNotifications: () => void;
  unread: boolean;
}) {
  return (
    <header className="topbar">
      <button className="brand" onClick={onHome} aria-label="AsliKaun home">
        <span className="brand-mark">
          <span />
          <span />
          <span />
        </span>
        AsliKaun
      </button>
      <div className="header-actions">
        <button
          className="icon-button notification-button"
          onClick={onNotifications}
          aria-label="Open notifications"
        >
          <Icon name="bell" />
          {unread && <span className="notification-dot" />}
        </button>
        <div className="avatar" aria-label="Driver Kavya Sharma">
          KS
        </div>
      </div>
    </header>
  );
}

function PageHead({
  title,
  text,
  back,
  eyebrow,
}: {
  title: string;
  text?: string;
  back?: () => void;
  eyebrow?: string;
}) {
  return (
    <div className="page-head">
      {back && (
        <button className="back-button" onClick={back}>
          <Icon name="back" size={18} />
          Back
        </button>
      )}
      {eyebrow && <div className="eyebrow">{eyebrow}</div>}
      <h1>{title}</h1>
      {text && <p>{text}</p>}
    </div>
  );
}

function Status({
  tone = "success",
  children,
}: {
  tone?: "success" | "warning" | "error" | "neutral";
  children: React.ReactNode;
}) {
  return (
    <span className={`status ${tone}`}>
      <i />
      {children}
    </span>
  );
}

function BottomNav({
  active,
  navigate,
}: {
  active: Tab;
  navigate: (tab: Tab) => void;
}) {
  const items: [Tab, string, string][] = [
    ["home", "Home", "home"],
    ["orders", "Orders", "orders"],
    ["earnings", "Earnings", "wallet"],
    ["profile", "Profile", "user"],
  ];
  return (
    <nav className="bottom-nav" aria-label="Primary navigation">
      {items.map(([tab, label, icon]) => (
        <button
          key={tab}
          className={active === tab ? "active" : ""}
          onClick={() => navigate(tab)}
        >
          <span>
            <Icon name={icon} />
          </span>
          {label}
        </button>
      ))}
    </nav>
  );
}

function Availability({
  online,
  setOnline,
}: {
  online: boolean;
  setOnline: (online: boolean) => void;
}) {
  return (
    <section className={`availability ${online ? "online" : ""}`}>
      <div>
        <div className="availability-label">
          <Status tone={online ? "success" : "neutral"}>
            {online ? "Online" : "Offline"}
          </Status>
          <span>Availability</span>
        </div>
        <h2>{online ? "You're ready for orders" : "You're currently offline"}</h2>
        <p>
          {online
            ? "We'll show new delivery requests as they arrive."
            : "Go online when you're ready to start delivering."}
        </p>
      </div>
      <button
        className={`availability-toggle ${online ? "on" : ""}`}
        onClick={() => setOnline(!online)}
        role="switch"
        aria-checked={online}
        aria-label={online ? "Go offline" : "Go online"}
      >
        <span />
      </button>
    </section>
  );
}

function SummaryCard({
  icon,
  value,
  label,
}: {
  icon: string;
  value: string;
  label: string;
}) {
  return (
    <div className="summary-mini">
      <Icon name={icon} size={17} />
      <strong>{value}</strong>
      <span>{label}</span>
    </div>
  );
}

function OrderCard({
  order,
  status,
  onClick,
  compact = false,
}: {
  order: Order;
  status: string;
  onClick: () => void;
  compact?: boolean;
}) {
  return (
    <button
      className={`order-card ${compact ? "compact" : ""}`}
      onClick={onClick}
    >
      <div className="order-topline">
        <span>Order #{order.id}</span>
        <Status tone={status === "Completed" ? "success" : "warning"}>
          {status}
        </Status>
      </div>
      <div className="order-route">
        <div className="route-rail">
          <i />
          <span />
          <i />
        </div>
        <div>
          <small>Pickup</small>
          <strong>{order.pickup}</strong>
          <small>Drop-off</small>
          <strong>{order.dropoff}</strong>
        </div>
      </div>
      <div className="order-meta">
        <span>
          <Icon name="route" size={15} />
          {order.distance}
        </span>
        <span>
          <Icon name="clock" size={15} />
          {order.duration}
        </span>
        <strong>₹{order.payout}</strong>
        <Icon name="arrow" size={17} />
      </div>
    </button>
  );
}

function RequestCard({
  order,
  accept,
  decline,
}: {
  order: Order;
  accept: () => void;
  decline: () => void;
}) {
  return (
    <section className="request-card">
      <div className="request-banner">
        <div className="pulse">
          <Icon name="spark" size={18} />
        </div>
        <div>
          <span>New delivery request</span>
          <strong>₹{order.payout} estimated payout</strong>
        </div>
        <em>Demo</em>
      </div>
      <div className="request-route">
        <div>
          <small>Pickup from</small>
          <strong>{order.pickup}</strong>
          <p>{order.pickupAddress}</p>
        </div>
        <Icon name="arrow" />
        <div>
          <small>Deliver to</small>
          <strong>{order.dropoff}</strong>
          <p>{order.distance} · {order.duration}</p>
        </div>
      </div>
      <div className="request-actions">
        <Button variant="secondary" onClick={decline}>
          Decline
        </Button>
        <Button onClick={accept}>Accept delivery</Button>
      </div>
    </section>
  );
}

function Home({
  online,
  setOnline,
  activeOrder,
  stage,
  requestVisible,
  accept,
  decline,
  navigate,
  todayEarnings,
  completedCount,
  scenario,
}: {
  online: boolean;
  setOnline: (online: boolean) => void;
  activeOrder: Order | null;
  stage: number;
  requestVisible: boolean;
  accept: () => void;
  decline: () => void;
  navigate: (page: Page) => void;
  todayEarnings: number;
  completedCount: number;
  scenario: Scenario;
}) {
  return (
    <main className="content home-page">
      <section className="greeting">
        <div className="eyebrow">Good morning, Kavya</div>
        <h1>Ready for your<br />next delivery?</h1>
      </section>

      {scenario.network && (
        <div className="network-banner">
          <Icon name="alert" size={18} />
          <p>
            <strong>Connection is unstable.</strong> Some updates may take a
            little longer.
          </p>
        </div>
      )}

      {scenario.verification === "requested" && (
        <button
          className="account-banner"
          onClick={() => navigate("verification")}
        >
          <span>
            <Icon name="shield" />
          </span>
          <div>
            <strong>Account check requested</strong>
            <small>Complete it when you're safely stopped.</small>
          </div>
          <Icon name="arrow" size={17} />
        </button>
      )}

      <Availability online={online} setOnline={setOnline} />

      <div className="today-head">
        <h2>Today</h2>
        <span>Demo data</span>
      </div>
      <div className="summary-grid">
        <SummaryCard
          icon="check"
          value={String(completedCount)}
          label="Deliveries"
        />
        <SummaryCard
          icon="wallet"
          value={`₹${todayEarnings}`}
          label="Earnings"
        />
        <SummaryCard icon="clock" value="5h 20m" label="Active" />
      </div>

      <section className="home-section">
        <div className="section-title">
          <h2>{activeOrder ? "Current delivery" : "Delivery requests"}</h2>
          {activeOrder && (
            <button onClick={() => navigate("orders")}>View orders</button>
          )}
        </div>
        {activeOrder ? (
          <OrderCard
            order={activeOrder}
            status={stageCopy[stage]?.label ?? "Active"}
            onClick={() => navigate("order")}
          />
        ) : online && requestVisible ? (
          <RequestCard order={demoOrder} accept={accept} decline={decline} />
        ) : (
          <div className="empty-card">
            <span>
              <Icon name={online ? "check" : "route"} size={25} />
            </span>
            <h3>{online ? "You're all caught up." : "Go online to get started."}</h3>
            <p>
              {online
                ? "New delivery requests will appear here."
                : "Delivery requests are paused while you're offline."}
            </p>
            {!online && (
              <Button onClick={() => setOnline(true)}>Go online</Button>
            )}
          </div>
        )}
      </section>

      <section className="home-section">
        <div className="section-title">
          <h2>Quick actions</h2>
        </div>
        <div className="quick-actions">
          {[
            ["orders", "My orders", "orders"],
            ["wallet", "Earnings", "earnings"],
            ["help", "Help & support", "support"],
          ].map(([icon, label, page]) => (
            <button key={label} onClick={() => navigate(page as Page)}>
              <span>
                <Icon name={icon} />
              </span>
              {label}
            </button>
          ))}
        </div>
      </section>
    </main>
  );
}

function EmptyState({
  icon,
  title,
  text,
}: {
  icon: string;
  title: string;
  text: string;
}) {
  return (
    <div className="empty-state">
      <span>
        <Icon name={icon} size={28} />
      </span>
      <h2>{title}</h2>
      <p>{text}</p>
    </div>
  );
}

function Orders({
  activeOrder,
  stage,
  completed,
  openOrder,
}: {
  activeOrder: Order | null;
  stage: number;
  completed: Order[];
  openOrder: () => void;
}) {
  const [tab, setTab] = useState<"Active" | "Completed">("Active");
  return (
    <main className="content">
      <PageHead title="Your orders." text="Active tasks and delivery history." />
      <div className="segment-control">
        {(["Active", "Completed"] as const).map((item) => (
          <button
            key={item}
            className={tab === item ? "active" : ""}
            onClick={() => setTab(item)}
          >
            {item}
            <span>{item === "Active" ? (activeOrder ? 1 : 0) : completed.length}</span>
          </button>
        ))}
      </div>
      {tab === "Active" ? (
        activeOrder ? (
          <div className="order-list">
            <OrderCard
              order={activeOrder}
              status={stageCopy[stage]?.label ?? "Active"}
              onClick={openOrder}
            />
          </div>
        ) : (
          <EmptyState
            icon="orders"
            title="No active deliveries."
            text="Go online from Home to receive a new request."
          />
        )
      ) : (
        <div className="order-list">
          {completed.map((order) => (
            <OrderCard
              key={order.id}
              order={order}
              status="Completed"
              compact
              onClick={() => {}}
            />
          ))}
        </div>
      )}
    </main>
  );
}

function RouteStop({
  type,
  title,
  address,
  last,
}: {
  type: string;
  title: string;
  address: string;
  last?: boolean;
}) {
  return (
    <div className="route-stop">
      <div className="route-point">
        <i />
        {!last && <span />}
      </div>
      <div>
        <small>{type}</small>
        <strong>{title}</strong>
        <p>{address}</p>
      </div>
    </div>
  );
}

function OrderDetail({
  order,
  stage,
  progress,
  back,
  support,
}: {
  order: Order;
  stage: number;
  progress: () => void;
  back: () => void;
  support: () => void;
}) {
  const current = stageCopy[stage];
  const isNavigation = stage === 0 || stage === 3;
  return (
    <main className="content order-detail-page">
      <PageHead
        eyebrow={`Order #${order.id}`}
        title={current.label}
        text={current.detail}
        back={back}
      />
      <div className="map-placeholder">
        <div className="map-road road-one" />
        <div className="map-road road-two" />
        <div className="map-road road-three" />
        <div className="map-route-line" />
        <span className="map-pin pickup-pin">
          <Icon name="car" size={18} />
        </span>
        <span className="map-pin destination-pin">
          <Icon name="pin" size={18} />
        </span>
        <div className="map-label">Simulated route · {order.duration}</div>
      </div>
      <section className="route-card">
        <RouteStop
          type="Pickup"
          title={order.pickup}
          address={order.pickupAddress}
        />
        <RouteStop
          type="Drop-off"
          title={order.dropoff}
          address={order.dropoffAddress}
          last
        />
      </section>
      <section className="instruction-card">
        <div>
          <Icon name="orders" size={19} />
          <strong>Delivery instructions</strong>
        </div>
        <p>{order.instructions}</p>
      </section>
      <div className="task-progress">
        {stageCopy.map((item, index) => (
          <div
            key={item.label}
            className={index <= stage ? "complete" : ""}
          >
            <i>{index < stage ? <Icon name="check" size={11} /> : index + 1}</i>
            <span>{item.label}</span>
          </div>
        ))}
      </div>
      <div className="sticky-action">
        <Button
          icon={isNavigation ? "navigation" : "check"}
          onClick={progress}
        >
          {current.action}
        </Button>
        <button className="support-shortcut" onClick={support}>
          <Icon name="help" size={18} />
          Help
        </button>
      </div>
    </main>
  );
}

function Earnings({
  deliveryBonus,
}: {
  deliveryBonus: number;
}) {
  const [filter, setFilter] = useState<keyof typeof earningData>("Today");
  const data = earningData[filter];
  const total = data.total + (filter === "Today" ? deliveryBonus : deliveryBonus);
  return (
    <main className="content earnings-page">
      <PageHead title="Earnings." text="A clear view of what you've earned." />
      <div className="filters">
        {(Object.keys(earningData) as (keyof typeof earningData)[]).map(
          (item) => (
            <button
              key={item}
              className={filter === item ? "active" : ""}
              onClick={() => setFilter(item)}
            >
              {item}
            </button>
          ),
        )}
      </div>
      <section className="earnings-hero">
        <span>{filter}'s earnings</span>
        <h2>₹{total.toLocaleString("en-IN")}</h2>
        <p>Demo earnings · last updated just now</p>
        <div className="bar-chart">
          {data.bars.map((height, index) => (
            <div key={data.labels[index]}>
              <span style={{ height: `${height}%` }} />
              <small>{data.labels[index]}</small>
            </div>
          ))}
        </div>
      </section>
      <div className="earning-summary">
        <div>
          <span>
            <Icon name="check" size={17} />
          </span>
          <div>
            <strong>{data.deliveries + (deliveryBonus ? 1 : 0)}</strong>
            <small>Completed deliveries</small>
          </div>
        </div>
        <div>
          <span>
            <Icon name="gift" size={17} />
          </span>
          <div>
            <strong>₹{data.incentive}</strong>
            <small>Incentives & bonuses</small>
          </div>
        </div>
      </div>
      <section className="transactions">
        <div className="section-title">
          <h2>Recent transactions</h2>
        </div>
        {[
          ["Delivery · TM-4798", "Today, 2:18 PM", "+₹74"],
          ["Peak hour incentive", "Today, 12:30 PM", "+₹120"],
          ["Delivery · TM-4771", "Today, 11:46 AM", "+₹92"],
          ["Delivery · TM-4746", "Today, 10:12 AM", "+₹68"],
        ].map(([title, time, amount]) => (
          <div className="transaction-row" key={title}>
            <span>
              <Icon name={title.includes("incentive") ? "gift" : "wallet"} size={18} />
            </span>
            <div>
              <strong>{title}</strong>
              <small>{time}</small>
            </div>
            <b>{amount}</b>
          </div>
        ))}
      </section>
    </main>
  );
}

function MenuSection({
  title,
  items,
  navigate,
}: {
  title: string;
  items: [string, string, string, Page?][];
  navigate: (page: Page) => void;
}) {
  return (
    <section className="menu-section">
      <h2>{title}</h2>
      <div className="menu-card">
        {items.map(([icon, label, value, page]) => (
          <button key={label} onClick={() => page && navigate(page)}>
            <span>
              <Icon name={icon} size={19} />
            </span>
            <div>
              <strong>{label}</strong>
              <small>{value}</small>
            </div>
            <Icon name="arrow" size={17} />
          </button>
        ))}
      </div>
    </section>
  );
}

function Profile({
  navigate,
  openDemo,
  scenario,
  onLogout,
}: {
  navigate: (page: Page) => void;
  openDemo: () => void;
  scenario: Scenario;
  onLogout: () => void;
}) {
  return (
    <main className="content profile-page">
      <PageHead title="Profile." text="Account details and app settings." />
      <section className="profile-card">
        <div className="profile-avatar">KS</div>
        <div>
          <h2>Kavya Sharma</h2>
          <p>+91 ••••• ••482</p>
          <small>Driver ID · DEMO-2841</small>
        </div>
        <Status
          tone={
            scenario.verification === "requested" ||
            scenario.verification === "failed"
              ? "warning"
              : "success"
          }
        >
          {scenario.verification === "requested" ? "Check needed" : "Active"}
        </Status>
      </section>
      <MenuSection
        title="Driver details"
        navigate={navigate}
        items={[
          ["user", "Personal information", "Name and registered phone", "settings"],
          ["car", "Vehicle information", "Two-wheeler · MH 02 •• 1842", "settings"],
        ]}
      />
      <MenuSection
        title="App settings"
        navigate={navigate}
        items={[
          ["bell", "Notification preferences", "Delivery and account updates", "settings"],
          ["lock", "App permissions", "Location while delivering", "settings"],
          ["globe", "Language", "English", "settings"],
          ["shield", "Privacy information", "How data is used", "settings"],
        ]}
      />
      <MenuSection
        title="Support"
        navigate={navigate}
        items={[
          ["help", "Help and support", "Orders, payments, and account help", "support"],
          ["phone", "Device and session", "This device · active now", "settings"],
        ]}
      />
      <Button variant="danger" onClick={onLogout}>Sign out</Button>
      <button className="demo-entry" onClick={openDemo}>
        <Icon name="settings" size={15} />
        Prototype scenario controls
      </button>
      <p className="demo-note">Profile details are deterministic demo data.</p>
    </main>
  );
}

function Settings({
  back,
  title = "Settings",
}: {
  back: () => void;
  title?: string;
}) {
  const [location, setLocation] = useState(false);
  const [notifications, setNotifications] = useState(true);
  return (
    <main className="content settings-page">
      <PageHead
        title={title}
        text="Manage your delivery app preferences."
        back={back}
      />
      <section className="settings-card">
        <div className="setting-row">
          <span>
            <Icon name="bell" />
          </span>
          <div>
            <strong>Delivery notifications</strong>
            <small>Requests, reminders, and status updates</small>
          </div>
          <button
            className={`small-toggle ${notifications ? "on" : ""}`}
            onClick={() => setNotifications(!notifications)}
            role="switch"
            aria-checked={notifications}
          >
            <i />
          </button>
        </div>
        <div className="setting-row">
          <span>
            <Icon name="pin" />
          </span>
          <div>
            <strong>Location while delivering</strong>
            <small>Not allowed in this prototype</small>
          </div>
          <button
            className={`small-toggle ${location ? "on" : ""}`}
            onClick={() => setLocation(!location)}
            role="switch"
            aria-checked={location}
          >
            <i />
          </button>
        </div>
      </section>
      <div className="privacy-panel">
        <Icon name="shield" />
        <div>
          <strong>Privacy by design</strong>
          <p>
            AsliKaun uses necessary, permitted account and session context.
            It does not ask you to manually enter motion or touch patterns.
            Missing data alone is not treated as proof of misuse.
          </p>
        </div>
      </div>
      <section className="plain-copy">
        <h2>How account protection works</h2>
        <p>
          When supported by the delivery platform and your permissions,
          account context and derived behavioural features can help protect
          your account in the background. The app only shows an action when the
          backend requests one.
        </p>
      </section>
    </main>
  );
}

function Notifications({
  scenario,
  activeOrder,
  openOrder,
  verify,
  back,
}: {
  scenario: Scenario;
  activeOrder: Order | null;
  openOrder: () => void;
  verify: () => void;
  back: () => void;
}) {
  const items = useMemo(() => {
    const base = [
      {
        icon: "gift",
        title: "Peak hour incentive added",
        text: "₹120 has been added to today's earnings.",
        time: "12:30 PM",
        action: () => {},
      },
      {
        icon: "check",
        title: "Delivery completed",
        text: "Order #TM-4798 was delivered successfully.",
        time: "11:46 AM",
        action: () => {},
      },
      {
        icon: "chat",
        title: "Support tip",
        text: "Keep your delivery app updated for the best experience.",
        time: "Yesterday",
        action: () => {},
      },
    ];
    if (activeOrder) {
      base.unshift({
        icon: "orders",
        title: "Order accepted",
        text: `Order #${activeOrder.id} is ready to view.`,
        time: "Just now",
        action: openOrder,
      });
    }
    if (scenario.verification === "requested") {
      base.unshift({
        icon: "shield",
        title: "Account verification required",
        text: "Complete this when you're safely stopped.",
        time: "Just now",
        action: verify,
      });
    }
    if (scenario.verification === "success") {
      base.unshift({
        icon: "shield",
        title: "Verification completed",
        text: "Your verification result was received.",
        time: "Today",
        action: verify,
      });
    }
    return base;
  }, [scenario, activeOrder, openOrder, verify]);

  return (
    <main className="content notifications-page">
      <PageHead
        title="Notifications."
        text="Delivery, earnings, and important account updates."
        back={back}
      />
      <div className="notification-list">
        {items.map((item) => (
          <button key={item.title} onClick={item.action}>
            <span>
              <Icon name={item.icon} size={19} />
            </span>
            <div>
              <strong>{item.title}</strong>
              <p>{item.text}</p>
              <small>{item.time}</small>
            </div>
            <Icon name="arrow" size={17} />
          </button>
        ))}
      </div>
    </main>
  );
}

function Verification({
  initial,
  back,
  support,
  complete,
}: {
  initial: Scenario["verification"];
  back: () => void;
  support: () => void;
  complete: () => void;
}) {
  const [stage, setStage] = useState<
    "ready" | "safe" | "progress" | "success" | "failed" | "review"
  >(
    initial === "success"
      ? "success"
      : initial === "failed"
        ? "failed"
        : initial === "review"
          ? "review"
          : "ready",
  );
  const selfieInputRef = useRef<HTMLInputElement>(null);

  if (stage === "progress") {
    return (
      <main className="content verification-progress">
        <div className="verification-orbit">
          <Icon name="shield" size={32} />
        </div>
        <PageHead
          title="Checking your response…"
          text="Keep this screen open. This prototype uses a deterministic demo check."
        />
        <Button disabled>Verification in progress</Button>
      </main>
    );
  }
  if (stage === "success") {
    return (
      <Result
        icon="check"
        tone="success"
        title="You're verified."
        text="Your verification result has been received."
        action="Return to app"
        onAction={() => {
          complete();
          back();
        }}
      />
    );
  }
  if (stage === "failed") {
    return (
      <Result
        icon="alert"
        tone="error"
        title="We couldn't complete verification."
        text="You can try again or request help if you believe this is a mistake."
        action="Try again"
        onAction={() => setStage("safe")}
        secondary="Get help"
        onSecondary={support}
      />
    );
  }
  if (stage === "review") {
    return (
      <Result
        icon="clock"
        tone="warning"
        title="Your account is under review."
        text="A support specialist has the request. We'll notify you when the status changes."
        action="Return to app"
        onAction={back}
      />
    );
  }
  if (stage === "safe") {
    return (
      <main className="content verification-page">
        <PageHead
          eyebrow="Safety check"
          title="Are you stopped somewhere safe?"
          text="Never complete verification while riding. Pull over safely before you continue."
          back={() => setStage("ready")}
        />
        <section className="safe-card">
          <Icon name="alert" size={27} />
          <h2>Pause before you verify.</h2>
          <p>
            We'll ask for a quick selfie, checked against your enrolled
            reference photo. A clear match clears this automatically; anything
            else is sent to a human reviewer — your account is never
            auto-suspended on this alone.
          </p>
        </section>
        <input
          ref={selfieInputRef}
          type="file"
          accept="image/*"
          capture="user"
          style={{ display: "none" }}
          onChange={async (event) => {
            const file = event.target.files?.[0];
            event.target.value = "";
            if (!file) return;
            setStage("progress");
            try {
              const base64 = await new Promise<string>((resolve, reject) => {
                const reader = new FileReader();
                reader.onload = () => resolve((reader.result as string).split(",")[1] ?? "");
                reader.onerror = reject;
                reader.readAsDataURL(file);
              });
              const result = await respondToChallenge(base64);
              if (result?.outcome === "cleared") setStage("success");
              else if (result?.outcome === "human_review") setStage("review");
              else setStage("failed");
            } catch {
              setStage("failed");
            }
          }}
        />
        <Button onClick={() => selfieInputRef.current?.click()}>
          I'm safely stopped — take selfie
        </Button>
        <Button variant="ghost" onClick={back}>
          Do this later
        </Button>
      </main>
    );
  }
  return (
    <main className="content verification-page">
      <PageHead
        eyebrow="Account protection"
        title="Let's confirm it's you."
        text="We need one additional step to help protect your account."
        back={back}
      />
      <section className="verification-method">
        <span>
          <Icon name="phone" size={25} />
        </span>
        <div>
          <small>Selfie step-up</small>
          <h2>Face match confirmation</h2>
          <p>Checked against your enrolled reference photo on file.</p>
        </div>
      </section>
      <div className="privacy-panel">
        <Icon name="lock" />
        <div>
          <strong>Complete this safely</strong>
          <p>
            Start only when stopped somewhere safe. The result is linked to
            this account session.
          </p>
        </div>
      </div>
      <Button onClick={() => setStage("safe")}>Start verification</Button>
      <Button variant="ghost" onClick={support}>
        Need help?
      </Button>
    </main>
  );
}

function Result({
  icon,
  tone,
  title,
  text,
  action,
  onAction,
  secondary,
  onSecondary,
}: {
  icon: string;
  tone: string;
  title: string;
  text: string;
  action: string;
  onAction: () => void;
  secondary?: string;
  onSecondary?: () => void;
}) {
  return (
    <main className="content result-page">
      <div className={`result-icon ${tone}`}>
        <Icon name={icon} size={34} />
      </div>
      <PageHead title={title} text={text} />
      <Button onClick={onAction}>{action}</Button>
      {secondary && (
        <Button variant="secondary" onClick={onSecondary}>
          {secondary}
        </Button>
      )}
    </main>
  );
}

function Support({ back }: { back: () => void }) {
  const [category, setCategory] = useState("");
  const [message, setMessage] = useState("");
  const [state, setState] = useState<"form" | "sending" | "success" | "error">(
    "form",
  );
  if (state === "success") {
    return (
      <Result
        icon="check"
        tone="success"
        title="Your request was received."
        text="We'll send a notification when support responds."
        action="Back to app"
        onAction={back}
      />
    );
  }
  if (state === "error") {
    return (
      <Result
        icon="alert"
        tone="error"
        title="We couldn't send your request."
        text="Check your connection and try again."
        action="Try again"
        onAction={() => setState("form")}
        secondary="Cancel"
        onSecondary={back}
      />
    );
  }
  return (
    <main className="content support-page">
      <PageHead
        title="How can we help?"
        text="Get support with a delivery, payment, or account check."
        back={back}
      />
      <label className="field">
        <span>Help topic</span>
        <select value={category} onChange={(event) => setCategory(event.target.value)}>
          <option value="">Choose a topic</option>
          <option>Active delivery</option>
          <option>Payment or incentive</option>
          <option>Account verification</option>
          <option>App or device issue</option>
          <option>Something else</option>
        </select>
      </label>
      <label className="field">
        <span>Tell us what happened</span>
        <textarea
          value={message}
          maxLength={300}
          onChange={(event) => setMessage(event.target.value)}
          placeholder="Include only the details support needs"
        />
        <small>{message.length}/300</small>
      </label>
      <Button
        disabled={!category || !message || state === "sending"}
        onClick={() => {
          setState("sending");
          window.setTimeout(() => setState("success"), 800);
        }}
      >
        {state === "sending" ? "Sending…" : "Send to support"}
      </Button>
      <button className="simulate-error" onClick={() => setState("error")}>
        Prototype: simulate connection error
      </button>
    </main>
  );
}

function ScenarioSheet({
  active,
  choose,
  close,
}: {
  active: Scenario;
  choose: (scenario: Scenario) => void;
  close: () => void;
}) {
  return (
    <div className="sheet-layer" role="dialog" aria-modal="true">
      <button className="sheet-scrim" onClick={close} aria-label="Close" />
      <section className="scenario-sheet">
        <div className="sheet-handle" />
        <div className="sheet-head">
          <div>
            <span>Developer-only controls</span>
            <h2>Mock backend scenarios</h2>
            <p>
              These deterministic states do not represent live sensor
              collection or model inference.
            </p>
          </div>
          <button className="icon-button" onClick={close} aria-label="Close">
            <Icon name="close" />
          </button>
        </div>
        <div className="scenario-list">
          {scenarios.map((scenario, index) => (
            <button
              key={scenario.id}
              className={active.id === scenario.id ? "active" : ""}
              onClick={() => choose(scenario)}
            >
              <span>{String(index + 1).padStart(2, "0")}</span>
              <div>
                <strong>{scenario.label}</strong>
                <small>{scenario.message}</small>
              </div>
              {active.id === scenario.id && <Icon name="check" size={17} />}
            </button>
          ))}
        </div>
      </section>
    </div>
  );
}

function MockLogin({ onLogin }: { onLogin: () => void }) {
  const [driverId, setDriverId] = useState("DEMO-2841");
  const [pin, setPin] = useState("4821");
  const [loading, setLoading] = useState(false);

  const handleLogin = (e?: React.FormEvent) => {
    if (e) e.preventDefault();
    setLoading(true);
    setTimeout(() => {
      onLogin();
    }, 350);
  };

  return (
    <div className="login-page">
      <div className="login-brand-wrap">
        <span className="brand-mark">
          <span />
          <span />
          <span />
        </span>
        <span className="login-brand-title">AsliKaun</span>
      </div>

      <div className="login-header">
        <h1>Driver Sign In.</h1>
        <p>Continuous identity verification & partner delivery terminal.</p>
      </div>

      <div className="demo-account-box">
        <div className="demo-tag">
          <Icon name="check" size={13} />
          Pre-Configured Demo Account
        </div>
        <div className="demo-driver-row">
          <div className="demo-avatar">KS</div>
          <div className="demo-info">
            <h3>Kavya Sharma</h3>
            <p>ID: DEMO-2841 · +91 ••••• ••482</p>
            <small>● Two-Wheeler · Verified Partner</small>
          </div>
        </div>
        <Button variant="primary" onClick={() => handleLogin()} disabled={loading}>
          {loading ? "Signing in..." : "Continue as Kavya Sharma ↗"}
        </Button>
      </div>

      <div className="login-divider">or manual terminal login</div>

      <form onSubmit={handleLogin}>
        <div className="login-field">
          <label>Registered Phone / Driver ID</label>
          <input
            type="text"
            value={driverId}
            onChange={(e) => setDriverId(e.target.value)}
            placeholder="Driver ID or Mobile"
            required
          />
        </div>

        <div className="login-field">
          <label>Security PIN</label>
          <input
            type="password"
            value={pin}
            onChange={(e) => setPin(e.target.value)}
            placeholder="Security PIN"
            required
          />
        </div>

        <div style={{ marginTop: 14 }}>
          <Button variant="secondary" disabled={loading}>
            {loading ? "Verifying..." : "Sign In to Terminal"}
          </Button>
        </div>
      </form>

      <p className="login-footer-note">
        Continuous Identity Intelligence Active<br />
        Protected by AsliKaun behavioral telematics.
      </p>
    </div>
  );
}

export default function App() {
  const [isAuthenticated, setIsAuthenticated] = useState(false);
  const [page, setPage] = useState<Page>("home");
  const [lastTab, setLastTab] = useState<Tab>("home");
  const [online, setOnline] = useState(false);
  const [networkAvailable, setNetworkAvailable] = useState(navigator.onLine);
  const [queueHealth, setQueueHealth] = useState<QueueHealth>({ pending: 0, retrying: 0, oldest: null, nextRetryAt: null });
  const [signalDiagnostics, setSignalDiagnostics] = useState<Record<string, { available: boolean; detail?: string }>>({});
  const [requestVisible, setRequestVisible] = useState(true);
  const [activeOrder, setActiveOrder] = useState<Order | null>(null);
  const [stage, setStage] = useState(0);
  const [completed, setCompleted] = useState<Order[]>(initialCompleted);
  const [deliveryBonus, setDeliveryBonus] = useState(0);
  const [showScenarios, setShowScenarios] = useState(false);
  const [scenario, setScenario] = useState<Scenario>(() => {
    const stored = localStorage.getItem("aslikaun-driver-scenario");
    return scenarios.find((item) => item.id === stored) ?? scenarios[0];
  });

  useEffect(() => {
    localStorage.setItem("aslikaun-driver-scenario", scenario.id);
  }, [scenario]);

  useEffect(() => {
    window.scrollTo({ top: 0, behavior: "smooth" });
  }, [page]);

  useEffect(() => {
    void getQueueHealth().then(setQueueHealth);
    const onQueue = (event: Event) => setQueueHealth((event as CustomEvent<QueueHealth>).detail);
    const onSignal = (event: Event) => {
      const detail = (event as CustomEvent<{ signal: string; available: boolean; detail?: string }>).detail;
      setSignalDiagnostics((current) => ({ ...current, [detail.signal]: detail }));
    };
    const onOnline = () => setNetworkAvailable(true);
    const onOffline = () => setNetworkAvailable(false);
    window.addEventListener("aslikaun:queue-health", onQueue);
    window.addEventListener("aslikaun:signal-status", onSignal);
    window.addEventListener("online", onOnline); window.addEventListener("offline", onOffline);
    return () => {
      window.removeEventListener("aslikaun:queue-health", onQueue);
      window.removeEventListener("aslikaun:signal-status", onSignal);
      window.removeEventListener("online", onOnline); window.removeEventListener("offline", onOffline);
    };
  }, []);

  useEffect(() => {
    if (online && requestVisible) recordOrderOffer();
  }, [online, requestVisible]);

  // Poll the backend for a live mid-trip safe-pause escalation while
  // online, independent of the local demo `scenario` picker. A real
  // challenge routes the driver to the same Verification screen the
  // demo scenarios use, but driven by an actual backend decision.
  useEffect(() => {
    if (!online) return;
    const interval = setInterval(async () => {
      const status = await getChallengeStatus();
      if (status?.challenge_pending && page !== "verification") {
        navigate("verification");
      }
    }, 15000);
    return () => clearInterval(interval);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [online, page]);

  const handleAvailabilityChange = async (nextOnline: boolean) => {
    if (!nextOnline) {
      goOffline();
      setOnline(false);
      return;
    }
    // Obtain clear in-app consent before browser permissions or telemetry collection.
    const consent = window.confirm(
      "AsliKaun collects a coarse location zone, supported device-motion readings, and tap/swipe timing (plus touch pressure only if your device exposes it) while you are online. You can stop collection by going offline. Continue?"
    );
    if (!consent) return;
    // Separate, explicit opt-in for precise GPS + full touch-gesture paths
    // (asked every time, not bundled into the step above, so declining it
    // never blocks going online with standard coarse-only protection).
    const wantsPrecision = window.confirm(
      "Enable Enhanced Detection Mode? This sends your exact GPS location and full touch-gesture paths (not just coarse zone / tap timing) so account-handoff detection is more accurate. This data is used only for security scoring and is automatically purged after 30 days. You can decline and keep standard protection instead."
    );
    const precisionConsentRecorded = await recordPrecisionConsent(wantsPrecision);
    if (wantsPrecision && !precisionConsentRecorded) {
      window.alert("Enhanced detection could not record consent securely. Continuing with standard coarse-location protection.");
    }
    // Browser permission prompts are triggered only after the driver explicitly chooses Go Online.
    setOnline(true);
    await goOnlineAndIngest(wantsPrecision && precisionConsentRecorded);
  };

  const navigate = (next: Page) => {
    if (["home", "orders", "earnings", "profile"].includes(next)) {
      setLastTab(next as Tab);
    }
    setPage(next);
  };

  const accept = () => {
    void recordOrderAccept();
    setActiveOrder(demoOrder);
    setRequestVisible(false);
    setStage(0);
    setPage("order");
  };

  const progress = () => {
    if (stage < stageCopy.length - 1) {
      setStage(stage + 1);
      return;
    }
    if (activeOrder) {
      setCompleted((orders) => [
        { ...activeOrder, id: `${activeOrder.id}-C` },
        ...orders,
      ]);
      setDeliveryBonus((amount) => amount + activeOrder.payout);
    }
    setActiveOrder(null);
    setStage(0);
    setPage("home");
  };

  const chooseScenario = (next: Scenario) => {
    setScenario(next);
    setShowScenarios(false);
    if (
      next.verification === "requested" ||
      next.verification === "failed" ||
      next.verification === "review"
    ) {
      setPage("home");
      setLastTab("home");
    }
  };

  const showBottomNav = ["home", "orders", "earnings", "profile"].includes(page);
  const todayEarnings = earningData.Today.total + deliveryBonus;

  if (!isAuthenticated) {
    return (
      <div className="app">
        <MockLogin onLogin={() => setIsAuthenticated(true)} />
      </div>
    );
  }

  return (
    <div className="app">
      <Header
        onHome={() => navigate("home")}
        onNotifications={() => navigate("notifications")}
        unread={scenario.verification === "requested" || !!activeOrder}
      />
      {(online || queueHealth.pending > 0 || Object.keys(signalDiagnostics).length > 0) && (
        <section className="telemetry-diagnostics" aria-label="Telemetry connection and sensor diagnostics" aria-live="polite">
          <span className={`diagnostic-pill ${online ? "ok" : "muted"}`}>{online ? "Collection on" : "Collection paused"}</span>
          <span className={`diagnostic-pill ${networkAvailable ? "ok" : "warning"}`}>{networkAvailable ? "Online" : "Offline"}</span>
          <span className="diagnostic-pill">Queue {queueHealth.pending} pending · {queueHealth.retrying} retrying</span>
          {Object.entries(signalDiagnostics).filter(([, value]) => !value.available).slice(-3).map(([signal, value]) => (
            <span key={signal} className="diagnostic-issue" title={value.detail}>{signal}: {value.detail || "unavailable"}</span>
          ))}
        </section>
      )}
      {page === "home" && (
        <Home
          online={online}
          setOnline={handleAvailabilityChange}
          activeOrder={activeOrder}
          stage={stage}
          requestVisible={requestVisible}
          accept={accept}
          decline={() => setRequestVisible(false)}
          navigate={navigate}
          todayEarnings={todayEarnings}
          completedCount={8 + (deliveryBonus ? 1 : 0)}
          scenario={scenario}
        />
      )}
      {page === "orders" && (
        <Orders
          activeOrder={activeOrder}
          stage={stage}
          completed={completed}
          openOrder={() => navigate("order")}
        />
      )}
      {page === "order" && activeOrder && (
        <OrderDetail
          order={activeOrder}
          stage={stage}
          progress={progress}
          back={() => navigate("orders")}
          support={() => navigate("support")}
        />
      )}
      {page === "earnings" && <Earnings deliveryBonus={deliveryBonus} />}
      {page === "profile" && (
        <Profile
          navigate={navigate}
          openDemo={() => setShowScenarios(true)}
          scenario={scenario}
          onLogout={() => {
            setIsAuthenticated(false);
            setPage("home");
          }}
        />
      )}
      {page === "notifications" && (
        <Notifications
          scenario={scenario}
          activeOrder={activeOrder}
          openOrder={() => activeOrder && navigate("order")}
          verify={() => navigate("verification")}
          back={() => navigate(lastTab)}
        />
      )}
      {page === "verification" && (
        <Verification
          initial={scenario.verification}
          back={() => navigate("home")}
          support={() => navigate("support")}
          complete={() =>
            setScenario(
              scenarios.find((item) => item.id === "verified") ?? scenarios[0],
            )
          }
        />
      )}
      {page === "support" && <Support back={() => navigate(lastTab)} />}
      {page === "settings" && (
        <Settings back={() => navigate("profile")} />
      )}
      {showBottomNav && (
        <BottomNav active={page as Tab} navigate={navigate} />
      )}
      {showScenarios && (
        <ScenarioSheet
          active={scenario}
          choose={chooseScenario}
          close={() => setShowScenarios(false)}
        />
      )}
    </div>
  );
}
