import React, { useState, useEffect, useCallback, useRef } from 'react';
import { 
  RefreshCw, 
  Download, 
  ChevronRight,
  ShieldAlert,
  SlidersHorizontal,
  FileSpreadsheet,
  AlertCircle,
  CheckCircle2,
  Lock,
  Layers
} from 'lucide-react';
import { DriverRecord, TabKey, FilterCategory } from './types/fleet';
import { Header } from './components/Header';
import { NavigationTabs } from './components/NavigationTabs';
import { StatCards } from './components/StatCards';
import { TwoAxisQuadrantView } from './components/TwoAxisQuadrantView';
import { FleetTable } from './components/FleetTable';
import { GeoTelemetryCorridor } from './components/GeoTelemetryCorridor';
import { ThreatDistribution } from './components/ThreatDistribution';
import { InvestigateDrawer } from './components/modals/InvestigateDrawer';
import { ReviewImeiModal } from './components/modals/ReviewImeiModal';
import { TriggerLivenessModal } from './components/modals/TriggerLivenessModal';
import { FreezeSessionModal } from './components/modals/FreezeSessionModal';
import { CommandPalette } from './components/modals/CommandPalette';
import { OverviewScreen } from './components/screens/OverviewScreen';
import { InvestigationsScreen } from './components/screens/InvestigationsScreen';
import { ReviewQueueScreen, ReviewItem } from './components/screens/ReviewQueueScreen';
import { ScenarioSimulatorScreen } from './components/screens/ScenarioSimulatorScreen';
import { ModelPerformanceScreen } from './components/screens/ModelPerformanceScreen';
import { AuditLogsScreen } from './components/screens/AuditLogsScreen';
import { takeSseMessages } from './utils/sse';

export default function App() {
  const [activeTab, setActiveTab] = useState<TabKey>('fleet-monitor');
  const [activeFilter, setActiveFilter] = useState<FilterCategory>('all');
  const [drivers, setDrivers] = useState<DriverRecord[]>([]);
  const [reviewItems, setReviewItems] = useState<ReviewItem[]>([]);
  const [isLoading, setIsLoading] = useState<boolean>(true);
  const [backendError, setBackendError] = useState<string | null>(null);
  const [isBackendConnected, setIsBackendConnected] = useState<boolean>(false);
  const [isDemoFallback, setIsDemoFallback] = useState<boolean>(false);
  const [syncRateEvents, setSyncRateEvents] = useState<number>(1240);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [streamStatus, setStreamStatus] = useState<'connecting' | 'connected' | 'reconnecting'>('connecting');
  const [historyRefreshRevision, setHistoryRefreshRevision] = useState(0);
  const lastEventId = useRef('');
  const seenEventIds = useRef(new Set<string>());

  // Active Modals & Drawers
  const [selectedDriverForInvestigation, setSelectedDriverForInvestigation] = useState<DriverRecord | null>(null);
  const [selectedDriverForImei, setSelectedDriverForImei] = useState<DriverRecord | null>(null);
  const [selectedDriverForLiveness, setSelectedDriverForLiveness] = useState<DriverRecord | null>(null);
  const [selectedDriverForFreeze, setSelectedDriverForFreeze] = useState<DriverRecord | null>(null);
  const [isCommandPaletteOpen, setIsCommandPaletteOpen] = useState(false);

  // 1. Authentication check on mount (Item 6)
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const urlToken = params.get('auth_token');
    const urlEmail = params.get('user_email');
    const urlName = params.get('user_name');
    const urlProvider = params.get('provider') || 'google';

    if (urlToken) {
      sessionStorage.setItem('tm-auth', 'true');
      sessionStorage.setItem('tm-auth-token', urlToken);
      if (urlEmail) {
        sessionStorage.setItem('tm-auth-user', JSON.stringify({
          email: urlEmail,
          name: urlName || urlEmail.split('@')[0],
          provider: urlProvider,
        }));
      }
      window.history.replaceState({}, document.title, window.location.pathname);
    }

    const hasAuth = sessionStorage.getItem('tm-auth') === 'true';
    const token = sessionStorage.getItem('tm-auth-token');

    if (!hasAuth && !token) {
      // Unauthenticated access attempt — redirect immediately to sign-in page
      window.location.assign('/auth?mode=login');
      return;
    }

    if (token?.startsWith('google-oauth-session-') || token?.startsWith('demo-')) {
      setIsDemoFallback(true);
    }
  }, []);

  // Helper to map API drivers to DriverRecord
  const mapApiDrivers = useCallback((apiDrivers: any[]): DriverRecord[] => {
    return apiDrivers.map((apiD: any) => {
      const id = apiD.driver_id;
      const telemetry = apiD.latest_telemetry || {};
      const gps = telemetry.gps || null;
      const ctxScore = typeof apiD.context_score === 'number' ? Math.round(apiD.context_score * 100) : 0;
      const behScore = typeof apiD.behavior_score === 'number' ? Math.round(apiD.behavior_score * 100) : 0;
      const isHandoff = (apiD.decision || '').includes('challenge') || apiD.quadrant === 'handoff_signature';
      const isLegit = apiD.quadrant === 'legit_change';
      const isAmbiguous = apiD.quadrant === 'ambiguous' || (apiD.decision || '').includes('step_up');

      const hasContextScore = typeof apiD.context_score === 'number';
      const hasBehaviorScore = typeof apiD.behavior_score === 'number';
      const riskCategory = !hasContextScore || !hasBehaviorScore
        ? 'Unscored'
        : isHandoff
        ? 'High Risk'
        : isAmbiguous
        ? 'Medium Risk'
        : isLegit
        ? 'Low Risk'
        : 'Trusted';

      const riskScore = typeof apiD.behavior_score === 'number' ? Math.round((1 - apiD.behavior_score) * 100) : 0;

      const riskReason = (apiD.reasons && apiD.reasons.length > 0)
        ? apiD.reasons[0]
        : (isHandoff ? 'Unauthorized Account Handoff Signature' : 'Continuous Verification Normal');

      return {
        id,
        name: id,
        avatarFallback: id.slice(-2).toUpperCase(),
        avatarBg: isHandoff ? 'bg-rose-600' : isLegit ? 'bg-blue-600' : 'bg-emerald-600',
        vehicle: 'Not reported by telemetry',
        plate: 'Not reported',
        shiftStatus: (telemetry.vehicle_speed_mph || gps?.speed || 0) > 2 ? 'Active Trip' : 'Standby',
        shiftDuration: 'Not reported',
        hardwareFrom: undefined,
        hardwareTo: telemetry.device_id || 'Not reported',
        lineageType: isLegit ? 'swap' : 'single',
        lineageStatusText: telemetry.device_id || 'No device telemetry',
        lineageStatusType: isLegit ? 'declared' : (isHandoff ? 'critical' : 'verified'),
        contextScore: ctxScore,
        hasContextScore,
        contextLabel: ctxScore >= 60 ? 'STABLE' : 'LOW BASELINE',
        contextDetail: [telemetry.device_id, telemetry.zone].filter(Boolean).join(' · ') || 'No context details reported',
        behavioralScore: behScore,
        hasBehaviorScore,
        behavioralLabel: behScore >= 60 ? 'OPTIMAL' : 'CRITICAL',
        behavioralDetail: apiD.model_status || 'Backend score unavailable',
        riskScore,
        riskCategory,
        riskReason,
        recommendedAction: isHandoff ? 'Investigate' : isLegit ? 'Review IMEI' : 'Telemetry',
        imei: 'Not reported',
        macAddress: 'Not reported',
        simSerial: 'Not reported',
        carrier: 'Not reported',
        location: {
          lat: typeof gps?.lat === 'number' ? gps.lat : Number.NaN,
          lng: typeof gps?.lng === 'number' ? gps.lng : Number.NaN,
          corridor: telemetry.zone || (gps ? 'GPS position reported' : 'No GPS position reported'),
          speedMph: telemetry.vehicle_speed_mph ?? gps?.speed ?? 0,
          heading: gps?.heading ?? 0,
        },
        anomalyFlags: apiD.reasons || [],
        signalScores: apiD.signals || {},
        signalMetric: apiD.model_status === 'per_rider_conformal_fusion' ? 'p_value' : 'anomaly_score',
        safePauseInfo: apiD.safe_pause_info || null,
        adaptiveThreshold: apiD.threshold_info || null,
      };
    });
  }, []);

  // 2. Fetch fleet scores and review queue from backend (Item 1)
  const fetchFleetData = useCallback(async () => {
    setIsRefreshing(true);
    const token = sessionStorage.getItem('aslikaun-auth-token') || sessionStorage.getItem('tm-auth-token') || 'demo-admin-session-token';
    const headers: Record<string, string> = {
      'Authorization': `Bearer ${token}`
    };

    try {
      // 1. Fetch live fleet scores: GET /v1/fleet/scores
      const scoresRes = await fetch('/v1/fleet/scores', { headers });

      if (scoresRes.status === 401) {
        // Token expired or invalid
        if (!token.startsWith('demo-') && !token.startsWith('google-oauth-session-')) {
          ['aslikaun-auth', 'aslikaun-auth-token', 'aslikaun-auth-user', 'tm-auth', 'tm-auth-token', 'tm-auth-user'].forEach((k) => sessionStorage.removeItem(k));
          window.location.assign('/auth?mode=login&error=Session+expired.+Please+sign+in+again.');
          return;
        }
        setIsBackendConnected(false);
        return;
      }

      if (!scoresRes.ok) {
        throw new Error(`Failed to load fleet scores (HTTP ${scoresRes.status})`);
      }

      const scoresData = await scoresRes.json();
      const apiDrivers = scoresData.drivers || [];

      if (apiDrivers.length > 0) {
        setDrivers(mapApiDrivers(apiDrivers));
      } else {
        // Empty state from DB
        setDrivers([]);
      }

      // 2. Fetch live review queue: GET /v1/review-queue
      const reviewRes = await fetch('/v1/review-queue?status=all', { headers });
      if (reviewRes.ok) {
        const reviewData = await reviewRes.json();
        setReviewItems(reviewData.items || reviewData.reviews || []);
      } else {
        throw new Error(`Failed to load review queue (HTTP ${reviewRes.status})`);
      }

      setIsBackendConnected(true);
      setBackendError(null);
    } catch (err: any) {
      console.warn('Backend API connection error:', err);
      setIsBackendConnected(false);
      setBackendError(err.message || 'Unable to connect to FastAPI backend at /v1/fleet/scores');

      setDrivers([]);
      setReviewItems([]);
    } finally {
      setIsLoading(false);
      setIsRefreshing(false);
    }
  }, [mapApiDrivers]);

  // Initial load
  useEffect(() => {
    fetchFleetData();
  }, [fetchFleetData]);

  // Authenticated SSE stream with bounded backoff and Last-Event-ID replay.
  useEffect(() => {
    let cancelled = false;
    let controller: AbortController | null = null;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;
    let reconcileTimer: ReturnType<typeof setTimeout> | null = null;
    let retryDelay = 1000;
    const connect = async () => {
      if (cancelled) return;
      controller = new AbortController();
      try {
        const token = sessionStorage.getItem('tm-auth-token') || 'demo-admin-session-token';
        const headers: Record<string, string> = { Authorization: `Bearer ${token}` };
        if (lastEventId.current) headers['Last-Event-ID'] = lastEventId.current;
        const response = await fetch('/v1/events/stream', { headers, signal: controller.signal });
        if (!response.ok || !response.body) throw new Error(`SSE unavailable (${response.status})`);
        setStreamStatus('connected'); retryDelay = 1000;
        const reader = response.body.getReader(); const decoder = new TextDecoder(); let buffer = '';
        while (!cancelled) {
          const { value, done } = await reader.read(); if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const parsed = takeSseMessages(buffer); buffer = parsed.remainder;
          for (const message of parsed.messages) {
            const id = message.id;
            if (message.event && message.event !== 'fleet-event') continue;
            if (id && (id === lastEventId.current || seenEventIds.current.has(id))) continue;
            let eventPayload: { driver_id?: unknown; event_type?: unknown };
            try { eventPayload = JSON.parse(message.data); } catch { continue; }
            if (typeof eventPayload.driver_id !== 'string' || typeof eventPayload.event_type !== 'string') continue;
            if (id) {
              lastEventId.current = id;
              seenEventIds.current.add(id);
              if (seenEventIds.current.size > 1000) seenEventIds.current.delete(seenEventIds.current.values().next().value!);
            }
            setSyncRateEvents((count) => count + 1);
            setHistoryRefreshRevision((revision) => revision + 1);
            if (reconcileTimer) clearTimeout(reconcileTimer);
            reconcileTimer = setTimeout(() => void fetchFleetData(), 250); // Reconcile a burst of live events with the persisted snapshot.
          }
        }
      } catch (error) {
        if (cancelled || (error instanceof DOMException && error.name === 'AbortError')) return;
      }
      if (!cancelled) {
        setStreamStatus('reconnecting');
        retryTimer = setTimeout(connect, retryDelay);
        retryDelay = Math.min(30000, retryDelay * 2);
      }
    };
    void connect();
    return () => { cancelled = true; controller?.abort(); if (retryTimer) clearTimeout(retryTimer); if (reconcileTimer) clearTimeout(reconcileTimer); };
  }, [fetchFleetData]);

  // Handle Review Resolution (POST /v1/review-queue/{session_id}/resolve)
  const handleResolveReview = async (
    sessionId: number,
    resolution: 'cleared' | 'confirmed_handoff' | 'needs_follow_up',
    note?: string
  ) => {
    const token = sessionStorage.getItem('tm-auth-token') || 'demo-admin-session-token';
    try {
      const res = await fetch(`/v1/review-queue/${sessionId}/resolve`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${token}`
        },
        body: JSON.stringify({
          resolution,
          reviewer_note: note || `Resolved via console as ${resolution}`
        })
      });

      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `Resolution failed (${res.status})`);
      }

      // Refresh data
      await fetchFleetData();
    } catch (e: any) {
      console.error('Resolve error:', e);
      throw e;
    }
  };

  // Keyboard shortcut for Command Palette (⌘K)
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
        e.preventDefault();
        setIsCommandPaletteOpen((prev) => !prev);
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, []);

  // CSV Export
  const handleExportCSV = () => {
    const headers = [
      'Driver ID',
      'Name',
      'Vehicle',
      'License Plate',
      'Shift Status',
      'Hardware To',
      'Context Score',
      'Behavioral Score',
      'Risk Score',
      'Risk Category',
      'Risk Reason',
      'IMEI',
      'Corridor Location'
    ];

    const rows = drivers.map((d) => [
      d.id,
      `"${d.name}"`,
      `"${d.vehicle}"`,
      `"${d.plate}"`,
      d.shiftStatus,
      `"${d.hardwareTo}"`,
      d.contextScore,
      d.behavioralScore,
      d.riskScore,
      d.riskCategory,
      `"${d.riskReason}"`,
      d.imei,
      `"${d.location.corridor}"`
    ]);

    const csvContent = [headers.join(','), ...rows.map((r) => r.join(','))].join('\n');
    const blob = new Blob([csvContent], { type: 'text/csv;charset=utf-8;' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.setAttribute('href', url);
    link.setAttribute('download', `aslikaun_telemetry_export_${new Date().toISOString().slice(0, 10)}.csv`);
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
  };

  const handleApproveImei = (driverId: string) => {
    setDrivers((prev) =>
      prev.map((d) =>
        d.id === driverId
          ? {
              ...d,
              riskCategory: 'Trusted',
              riskScore: 14,
              riskReason: 'Hardware Swap Approved & Enrolled',
              lineageStatusText: 'Attestation verified by Threat Ops',
              lineageStatusType: 'verified',
            }
          : d
      )
    );
  };

  const handleLivenessSuccess = (driverId: string) => {
    setDrivers((prev) =>
      prev.map((d) =>
        d.id === driverId
          ? {
              ...d,
              riskCategory: 'Trusted',
              riskScore: 18,
              riskReason: 'Biometric Liveness Challenge Verified',
              livenessPending: false,
            }
          : d
      )
    );
    setSelectedDriverForLiveness(null);
  };

  const handleConfirmFreeze = (driverId: string) => {
    setDrivers((prev) =>
      prev.map((d) =>
        d.id === driverId
          ? {
              ...d,
              sessionFrozen: true,
              shiftStatus: 'Standby',
              riskReason: 'Session Locked Down by Threat Ops',
            }
          : d
      )
    );
    setSelectedDriverForFreeze(null);
    if (selectedDriverForInvestigation?.id === driverId) {
      setSelectedDriverForInvestigation((prev) => (prev ? { ...prev, sessionFrozen: true } : null));
    }
  };

  // Measured counts
  const highRiskCount = drivers.filter((d) => d.riskCategory === 'High Risk').length;
  const newHardwareCount = drivers.filter((d) => d.lineageType === 'swap').length;
  const openReviewCount = reviewItems.filter((r) => r.status === 'open').length;
  const scoredDrivers = drivers.filter((d) => d.hasContextScore !== false && d.hasBehaviorScore !== false);

  return (
    <div className="min-h-screen bg-[#f8f9ff] text-[#0b1c30] flex flex-col font-sans selection:bg-[#eff4ff] selection:text-[#0058be]">
      {/* Top Header */}
      <Header
        onOpenCommandPalette={() => setIsCommandPaletteOpen(true)}
        onSelectDriverById={(id) => {
          const found = drivers.find((d) => d.id === id);
          if (found) setSelectedDriverForInvestigation(found);
        }}
        unreadAlertsCount={openReviewCount}
      />

      {/* Backend Status Notification Strip */}
      <div className="w-full bg-white border-b border-[#e2e8f0] px-6 py-1.5 flex items-center justify-between text-xs">
        <div className="flex items-center gap-2">
          {isBackendConnected ? (
            <span className="inline-flex items-center gap-1.5 font-semibold text-[#006947]">
              <span className="w-2 h-2 rounded-full bg-[#006947] animate-pulse" />
              Connected to Live Backend API (FastAPI · Port 8000)
            </span>
          ) : isDemoFallback ? (
            <span className="inline-flex items-center gap-1.5 font-semibold text-[#0058be]">
              <span className="w-2 h-2 rounded-full bg-[#0058be]" />
              Demo Sandbox Mode (Local Authenticated Session)
            </span>
          ) : (
            <span className="inline-flex items-center gap-1.5 font-semibold text-[#d97706]">
              <span className="w-2 h-2 rounded-full bg-[#d97706]" />
              Offline Fallback · Local Evaluation Engine
            </span>
          )}
          <span className="text-[#cbd5e1]">|</span>
          <span className="text-[#565e74]" role="status" aria-live="polite" aria-label={`Fleet event stream ${streamStatus}`}>
            Live Drivers: <strong>{drivers.length}</strong> · Open HITL Reviews: <strong>{openReviewCount}</strong> · Events: <strong>{streamStatus}</strong>
          </span>
        </div>

        {backendError && (
          <div className="flex items-center gap-2 text-[#ba1a1a]">
            <AlertCircle className="w-3.5 h-3.5" />
            <span className="text-[11px] truncate max-w-md">{backendError}</span>
            <button
              onClick={fetchFleetData}
              className="text-[11px] underline font-bold hover:text-[#93000a] cursor-pointer"
            >
              Retry
            </button>
          </div>
        )}
      </div>

      {/* Secondary Navigation Tabs */}
      <NavigationTabs
        activeTab={activeTab}
        onSelectTab={setActiveTab}
        investigationCount={highRiskCount}
              reviewQueueCount={openReviewCount}
      />

      {/* Main Workspace Body */}
      <main className="flex-1 max-w-[1440px] w-full mx-auto px-6 py-6">
        {/* Loading State */}
        {isLoading && drivers.length === 0 ? (
          <div className="py-24 text-center space-y-3">
            <div className="w-8 h-8 border-3 border-[#0058be] border-t-transparent rounded-full animate-spin mx-auto" />
            <h3 className="text-base font-bold text-[#0b1c30]">Connecting to Telemetry Stream...</h3>
            <p className="text-xs text-[#565e74]">
              Querying GET /v1/fleet/scores and GET /v1/review-queue with secure bearer credentials.
            </p>
          </div>
        ) : (
          <>
            {activeTab === 'fleet-monitor' && (
              <div className="space-y-6">
                {/* Breadcrumb & Screen Title Bar */}
                <div className="flex flex-col md:flex-row md:items-center justify-between gap-4">
                  <div>
                    <div className="flex items-center gap-2 text-xs font-semibold text-[#727785] tracking-wide mb-1">
                      <span>FLEET CORE MESH</span>
                      <span>/</span>
                      <span className="text-[#0058be]">Telemetry Protocol L4</span>
                    </div>
                    <h1 className="text-2xl md:text-3xl font-bold tracking-tight text-[#0b1c30]">
                      Fleet Telemetry &amp; Driver Monitor
                    </h1>
                    <p className="text-xs md:text-sm text-[#565e74] mt-1 max-w-4xl">
                      Continuous verification stream populated live from <code className="font-mono text-[#0058be]">GET /v1/fleet/scores</code>. Two-axis context and behavioral biometrics with pause-aware challenge timing.
                    </p>
                  </div>

                  {/* Action Buttons: Refresh Feed + Export CSV */}
                  <div className="flex items-center gap-3 shrink-0 self-start md:self-auto">
                    <button
                      onClick={fetchFleetData}
                      disabled={isRefreshing}
                      className="px-4 py-2 bg-white hover:bg-[#f8f9ff] text-[#0b1c30] text-xs font-semibold rounded-md border border-[#e2e8f0] shadow-2xs flex items-center gap-2 transition-colors cursor-pointer disabled:opacity-50"
                    >
                      <RefreshCw className={`w-3.5 h-3.5 text-[#565e74] ${isRefreshing ? 'animate-spin text-[#0058be]' : ''}`} />
                      <span>{isRefreshing ? 'Refreshing...' : 'Refresh Feed'}</span>
                    </button>
                    <button
                      onClick={handleExportCSV}
                      className="px-4 py-2 bg-[#0058be] hover:bg-[#0047a0] text-white text-xs font-semibold rounded-md shadow-xs flex items-center gap-2 transition-colors cursor-pointer"
                    >
                      <Download className="w-3.5 h-3.5" />
                      <span>Export CSV</span>
                    </button>
                  </div>
                </div>

                {/* 4 Metric KPI Stat Cards */}
                <StatCards
                  activeStreams={drivers.length}
                  severeCount={highRiskCount}
                  newHardwareCount={newHardwareCount}
                  livenessGateCount={openReviewCount}
                  onFilterClick={setActiveFilter}
                />

                {/* Two-Axis Trust Quadrant Plane (Item 2) */}
                <TwoAxisQuadrantView
                  drivers={scoredDrivers}
                  selectedDriverId={selectedDriverForInvestigation?.id}
                  onSelectDriver={(driver) => setSelectedDriverForInvestigation(driver)}
                  onRefresh={fetchFleetData}
                  isRefreshing={isRefreshing}
                />

                {/* Main Fleet Telemetry Table */}
                <FleetTable
                  drivers={drivers}
                  activeFilter={activeFilter}
                  onFilterChange={setActiveFilter}
                  onInvestigate={(driver) => setSelectedDriverForInvestigation(driver)}
                  onReviewImei={(driver) => setSelectedDriverForImei(driver)}
                  onFreezeSession={(driver) => setSelectedDriverForFreeze(driver)}
                  onTriggerLiveness={(driver) => setSelectedDriverForLiveness(driver)}
                  onOpenTelemetry={(driver) => setSelectedDriverForInvestigation(driver)}
                  syncRateEvents={syncRateEvents}
                />

                {/* Bottom 2 Columns: Geo-Telemetry Corridor & Threat Distribution */}
                <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
                  <GeoTelemetryCorridor
                    drivers={drivers}
                    onSelectDriver={(driver) => setSelectedDriverForInvestigation(driver)}
                  />
                  <ThreatDistribution
                    onOpenSimulator={() => setActiveTab('scenario-simulator')}
                  />
                </div>
              </div>
            )}

            {/* Alternate Screens for navigation tabs */}
            {activeTab === 'overview' && (
              <div className="space-y-6">
                <TwoAxisQuadrantView
                  drivers={drivers}
                  selectedDriverId={selectedDriverForInvestigation?.id}
                  onSelectDriver={(driver) => setSelectedDriverForInvestigation(driver)}
                  onRefresh={fetchFleetData}
                  isRefreshing={isRefreshing}
                />
                <OverviewScreen
                  drivers={drivers}
                  onNavigateToFleet={() => setActiveTab('fleet-monitor')}
                />
              </div>
            )}

            {activeTab === 'investigations' && (
              <InvestigationsScreen
                drivers={drivers}
                onInvestigateDriver={(driver) => setSelectedDriverForInvestigation(driver)}
              />
            )}

            {activeTab === 'review-queue' && (
              <ReviewQueueScreen
                drivers={drivers}
                reviewItems={reviewItems}
                onReviewImei={(driver) => setSelectedDriverForImei(driver)}
                onResolveReview={handleResolveReview}
                onRefresh={fetchFleetData}
              />
            )}

            {activeTab === 'scenario-simulator' && (
              <ScenarioSimulatorScreen
                drivers={drivers}
                onRefreshFleet={fetchFleetData}
                onNavigateToFleet={() => setActiveTab('fleet-monitor')}
              />
            )}

            {activeTab === 'model-performance' && (
              <ModelPerformanceScreen />
            )}

            {activeTab === 'audit-logs' && (
              <AuditLogsScreen />
            )}
          </>
        )}
      </main>

      {/* Global Bottom Status Bar / Footer */}
      <footer className="w-full bg-white border-t border-[#e2e8f0] px-6 py-3 mt-12 text-xs text-[#565e74] flex flex-col sm:flex-row items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <span className="font-semibold text-[#0b1c30]">
            AsliKaun Security OS v3.4-prod
          </span>
          <span className="text-[#c2c6d6]">·</span>
          <span className="inline-flex items-center gap-1.5 text-[#006947] font-medium">
            <span className="w-1.5 h-1.5 rounded-full bg-[#006947]" />
            Continuous Multi-Modal Verification Active
          </span>
        </div>
        <div className="flex items-center gap-6">
          <button 
            onClick={() => setActiveTab('model-performance')}
            className="text-[#565e74] hover:text-[#0b1c30] transition-colors cursor-pointer"
          >
            Model Observability &amp; Fairness
          </button>
          <button 
            onClick={() => setActiveTab('investigations')}
            className="text-[#565e74] hover:text-[#0b1c30] transition-colors cursor-pointer"
          >
            SOC Runbooks
          </button>
          <button 
            onClick={() => setActiveTab('scenario-simulator')}
            className="text-[#565e74] hover:text-[#0b1c30] transition-colors cursor-pointer"
          >
            Threat Lab Simulator
          </button>
        </div>
      </footer>

      {/* Modals & Slide-over Drawers */}
      <InvestigateDrawer
        driver={selectedDriverForInvestigation}
        refreshRevision={historyRefreshRevision}
        onClose={() => setSelectedDriverForInvestigation(null)}
        onFreezeSession={(driver) => setSelectedDriverForFreeze(driver)}
        onTriggerLiveness={(driver) => setSelectedDriverForLiveness(driver)}
      />

      <ReviewImeiModal
        driver={selectedDriverForImei}
        onClose={() => setSelectedDriverForImei(null)}
        onApprove={handleApproveImei}
      />

      <TriggerLivenessModal
        driver={selectedDriverForLiveness}
        onClose={() => setSelectedDriverForLiveness(null)}
        onSuccess={handleLivenessSuccess}
      />

      <FreezeSessionModal
        driver={selectedDriverForFreeze}
        onClose={() => setSelectedDriverForFreeze(null)}
        onConfirmFreeze={handleConfirmFreeze}
      />

      <CommandPalette
        isOpen={isCommandPaletteOpen}
        onClose={() => setIsCommandPaletteOpen(false)}
        drivers={drivers}
        onSelectDriver={(driver) => setSelectedDriverForInvestigation(driver)}
        onNavigateTab={(tab) => setActiveTab(tab)}
      />
    </div>
  );
}
