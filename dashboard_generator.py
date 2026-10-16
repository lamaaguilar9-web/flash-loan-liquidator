"""
Flash Loan Liquidator (Module C) - Live HTML Dashboard Generator.

Renders an auto-refreshing institutional dark-mode HTML dashboard showing the live
telemetry of the 5-in-1 Commission Engine, Chainlink oracles, DEX spreads,
and the Central Profit Priority Queue.
"""

import os
from datetime import datetime, timezone
from typing import Dict, Any, Optional

DEFAULT_DESKTOP_DASHBOARD = r"C:\Users\luis\Desktop\TABLERO_MOTOR_5_EN_1.html"
DEFAULT_LOCAL_DASHBOARD = os.path.join(os.path.dirname(__file__), "data", "dashboard_live.html")


def render_dashboard_html(summary: Dict[str, Any], telegram_configured: bool = False) -> str:
    """Generates the complete HTML document for the 5-in-1 Commission Engine."""
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    chains = summary.get("chains", {})
    oracles = summary.get("oracles", {})
    arb = summary.get("live_dex_arb_check") or {}
    queue = summary.get("stress_priority_queue") or summary.get("live_priority_queue") or []

    chain_rows = ""
    for cname, cdata in chains.items():
        status_badge = (
            '<span class="badge ok">ONLINE</span>'
            if cdata.get("block_number")
            else '<span class="badge warn">OFFLINE</span>'
        )
        chain_rows += f"""
        <tr>
            <td><strong>{cname}</strong></td>
            <td>{status_badge}</td>
            <td><code>{cdata.get('block_number')}</code></td>
            <td>{cdata.get('gas_price_gwei')} Gwei</td>
            <td>{cdata.get('rpc_latency_ms')} ms</td>
        </tr>
        """

    oracle_rows = ""
    for cname, feeds in oracles.items():
        for sym, fdata in feeds.items():
            badge = (
                '<span class="badge ok">VALID</span>'
                if fdata.get("is_valid")
                else f'<span class="badge err">{fdata.get("reason")}</span>'
            )
            oracle_rows += f"""
            <tr>
                <td>{cname}</td>
                <td><strong>{sym}</strong></td>
                <td>${fdata.get('price_usd')}</td>
                <td><code>{fdata.get('round_id')}</code></td>
                <td>{fdata.get('age_seconds')} s</td>
                <td>{badge}</td>
            </tr>
            """

    bot_descriptions = {
        "BOT_1_FLASH_LIQUIDATION": "Liquidación con Flash Loan (Venus / Kinza / Aave)",
        "BOT_2_DEX_FLASH_ARBITRAGE": "Arbitraje Atómico DEX-a-DEX (PancakeSwap V3 ↔ V2/Thena)",
        "BOT_3_AUCTION_KICK_KEEPER": "Bounty Inicio de Subasta Holandesa kick() (Lista DAO)",
        "BOT_4_VAULT_HARVEST_KEEPER": "Cosechador de Bóveda harvest() Call-Fee (Beefy Finance)",
        "BOT_5_PERP_ORDER_KEEPER": "Keeper Órdenes Límite y Perpetuos (ApolloX / GMX)"
    }

    queue_rows = ""
    for idx, item in enumerate(queue, 1):
        sid = item.get("strategy_id", "BOT")
        desc = bot_descriptions.get(sid, sid)
        queue_rows += f"""
        <tr>
            <td><strong>#{idx}</strong></td>
            <td><code>{sid}</code></td>
            <td>{desc}</td>
            <td>{item.get('chain', 'BNB')}</td>
            <td>${item.get('gas_cost_usd', 0.0):.4f}</td>
            <td class="profit">+${item.get('net_profit_usd', 0.0):.4f} USD</td>
            <td><span class="badge ok">LISTO EN COLA #{idx}</span></td>
        </tr>
        """

    tg_badge = (
        '<span class="badge ok">CONECTADO (@Luis_sentinel_alert_bot)</span>'
        if telegram_configured
        else '<span class="badge info">LISTO (Activar TELEGRAM_BOT_TOKEN en .env)</span>'
    )

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta http-equiv="refresh" content="15">
    <title>Sentinel Fleet — Tablero en Vivo Motor 5-en-1 de Comisiones</title>
    <style>
        body {{ background: #0b0f19; color: #e2e8f0; font-family: 'Segoe UI', system-ui, sans-serif; margin: 0; padding: 24px; }}
        .container {{ max-width: 1200px; margin: 0 auto; }}
        .header {{ display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #1e293b; padding-bottom: 16px; margin-bottom: 24px; }}
        h1 {{ margin: 0; font-size: 22px; color: #38bdf8; }}
        h2 {{ font-size: 16px; color: #94a3b8; margin-top: 24px; text-transform: uppercase; letter-spacing: 0.5px; }}
        .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 16px; margin-bottom: 24px; }}
        .card {{ background: #111827; border: 1px solid #1f2937; border-radius: 10px; padding: 16px; }}
        .card-title {{ font-size: 12px; color: #94a3b8; text-transform: uppercase; }}
        .card-value {{ font-size: 20px; font-weight: 700; margin-top: 6px; color: #f8fafc; }}
        table {{ width: 100%; border-collapse: collapse; background: #111827; border-radius: 10px; overflow: hidden; border: 1px solid #1f2937; }}
        th, td {{ padding: 11px 14px; text-align: left; border-bottom: 1px solid #1f2937; font-size: 13.5px; }}
        th {{ background: #1e293b; color: #cbd5e1; font-weight: 600; }}
        .badge {{ padding: 3px 8px; border-radius: 6px; font-size: 11.5px; font-weight: 600; }}
        .ok {{ background: #064e3b; color: #34d399; }}
        .warn {{ background: #78350f; color: #fbbf24; }}
        .err {{ background: #7f1d1d; color: #f87171; }}
        .info {{ background: #1e3a8a; color: #93c5fd; }}
        .profit {{ color: #34d399; font-weight: 700; }}
        code {{ background: #1e293b; padding: 2px 6px; border-radius: 4px; color: #38bdf8; }}
    </style>
</head>
<body>
<div class="container">
    <div class="header">
        <div>
            <h1>🛡️ SENTINEL FLEET — MOTOR ÚNICO 5-EN-1 DE COMISIONES ON-CHAIN</h1>
            <div style="font-size:13px;color:#94a3b8;margin-top:4px;">Actualización automática cada 15s | Último corte: {now_utc}</div>
        </div>
        <div>{tg_badge}</div>
    </div>

    <div class="grid">
        <div class="card">
            <div class="card-title">Modo Operativo Actual</div>
            <div class="card-value" style="color:#38bdf8;">MODO SOMBRA (SOLO LECTURA)</div>
        </div>
        <div class="card">
            <div class="card-title">Contrato Fase 2 (Solidity 0.8.20)</div>
            <div class="card-value" style="color:#34d399;">COMPILADO (8,130 bytes)</div>
        </div>
        <div class="card">
            <div class="card-title">Spread DEX en Vivo (WBNB/USDT V2 vs V3)</div>
            <div class="card-value">{arb.get('spread_bps', 0.0)} bps (${arb.get('buy_price_usd', 0)} → ${arb.get('sell_price_usd', 0)})</div>
        </div>
        <div class="card">
            <div class="card-title">Préstamos Escaneados / En Vigilancia</div>
            <div class="card-value">{summary.get('positions_scanned_count', 0)} escaneados / {summary.get('watchlist_count', 0)} alerta</div>
        </div>
    </div>

    <h2>1. Cola Central de Prioridad — Los 5 Bots en 1 Solo Motor</h2>
    <table>
        <thead>
            <tr>
                <th>Orden</th>
                <th>ID Bot</th>
                <th>Estrategia de Comisión</th>
                <th>Red</th>
                <th>Gas Real USD</th>
                <th>Ganancia Neta Simulada</th>
                <th>Estado</th>
            </tr>
        </thead>
        <tbody>{queue_rows}</tbody>
    </table>

    <h2>2. Estado de Redes y Bloques en Vivo (Mainnet)</h2>
    <table>
        <thead>
            <tr>
                <th>Red</th>
                <th>Estado</th>
                <th>Bloque Actual</th>
                <th>Gas Price</th>
                <th>Latencia RPC</th>
            </tr>
        </thead>
        <tbody>{chain_rows}</tbody>
    </table>

    <h2>3. Oráculos Chainlink Verificados en Vivo (Fail-Closed)</h2>
    <table>
        <thead>
            <tr>
                <th>Red</th>
                <th>Par</th>
                <th>Precio USD</th>
                <th>Round ID</th>
                <th>Edad</th>
                <th>Veredicto</th>
            </tr>
        </thead>
        <tbody>{oracle_rows}</tbody>
    </table>
</div>
</body>
</html>"""


def write_dashboard_files(
    summary: Dict[str, Any],
    telegram_configured: bool = False,
    desktop_path: Optional[str] = DEFAULT_DESKTOP_DASHBOARD,
    local_path: str = DEFAULT_LOCAL_DASHBOARD
) -> str:
    """Writes the live HTML dashboard to data/dashboard_live.html and to the CEO's Desktop."""
    html = render_dashboard_html(summary, telegram_configured=telegram_configured)
    os.makedirs(os.path.dirname(local_path), exist_ok=True)
    with open(local_path, "w", encoding="utf-8") as f:
        f.write(html)

    if desktop_path:
        try:
            os.makedirs(os.path.dirname(desktop_path), exist_ok=True)
            with open(desktop_path, "w", encoding="utf-8") as f:
                f.write(html)
        except Exception:
            pass
    return html
