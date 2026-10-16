# Sentinel Fleet — Flash Loan Liquidator (Módulo C — Fase 1: Modo Sombra)

Escáner determinista de solo lectura (*Shadow Mode*) para la detección y simulación de rentabilidad neta de liquidaciones atómicas con *Flash Loans* en protocolos de préstamos sobre **BNB Chain** (Venus Protocol, Kinza Finance) y **Arbitrum One** (Aave V3).

> **Estándar de Verificación:** Código auditado internamente con doble verificación independiente — un auditor línea a línea y una segunda opinión por muestreo, con la evidencia pública en el repositorio.

## Arquitectura y Candados *Fail-Closed*

1. **`oracle_watcher.py`**:
   * Consulta en vivo contratos agregadores de **Chainlink** (`latestRoundData` `0xfeaf968c` y `decimals` `0x313ce567`).
   * Verifica la liquidez real en rango (`liquidity` `0x1a686502` y `slot0` `0x3850c7bd`) en **PancakeSwap V3**.
   * **Candado Fail-Closed:** Rechaza rondas incompletas (`answeredInRound < roundId`), respuestas $\le 0$, o precios desactualizados (`age > max_staleness_seconds`).
   * **Garantía de Solo Lectura:** El cliente RPC bloquea por código (`PermissionError`) cualquier método de escritura (`eth_sendRawTransaction`, etc.).
2. **`position_indexer.py`**:
   * Descubrimiento de prestatarios mediante lectura de eventos recientes (`eth_getLogs`) y semillas verificadas on-chain.
   * Decodificación nativa de `getUserAccountData(address)` (`0xbf92857c`, arquitectura Aave V3 / Kinza) y `getAccountLiquidity(address)` (`0x5ec88c79`, arquitectura Compound V2 / Venus).
3. **`profit_simulator.py`**:
   * Cálculo exacto de ganancia neta simulada:
     $$\text{PnL Neto} = \text{Bono Bruto de Liquidación} - \text{Comisión Flash Loan} - \text{Costo Swap DEX + Slippage Dinámico} - \text{Costo Gas USD}$$
   * Si $\text{PnL Neto} < \text{Umbral Mínimo}$ (`$1.50 USD`) o si la posición es insolvente ($\text{Colateral Total} < \text{Deuda a Cubrir}$), simula la reversión atómica con `$0.00` de pérdida de capital.
4. **`shadow_logger.py`**:
   * Bitácora apendizable en `data/shadow_liquidations.jsonl` y `data/shadow_telemetry.jsonl` con sello de integridad `record_sha256` por registro y separación estricta de banderas `is_test`.

## Ejecución

```bash
# Ejecutar suite de pruebas (12 tests deterministas)
pytest -v

# Ejecutar un ciclo de escaneo en vivo (BNB Chain + Arbitrum) con prueba de estrés de parámetros reales
python shadow_scanner.py --chains BNB ARBITRUM --cycles 1 --simulate-stress
```
