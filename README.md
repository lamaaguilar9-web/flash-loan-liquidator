# Sentinel Fleet — Motor Único 5-en-1 de Comisiones On-Chain (`flash-loan-liquidator`)

Motor unificado en Modo Sombra (**Fase 1 — Python**) y contrato inteligente ejecutor polimórfico atómico (**Fase 2 — Solidity `0.8.20`**) en **BNB Chain** y **Arbitrum One**.

> **Alcance Operativo Oficial en Vivo (`M3`):**
> **2 evaluadores económicos en vivo (liquidaciones Venus/Kinza/Aave + arbitraje DEX) + 3 monitores on-chain en vivo (Lista/Beefy/GMX).**

1. **Bot #1 (`BOT_1_FLASH_LIQUIDATION`) — [Evaluador Económico en Vivo]:** Liquidaciones con Flash Loans en Venus Protocol (*HF estimado derivado de `getAccountLiquidity`*), Kinza Finance y Aave V3 (*HF nativo on-chain de `getUserAccountData`*).
2. **Bot #2 (`BOT_2_DEX_FLASH_ARBITRAGE`) — [Evaluador Económico en Vivo]:** Arbitraje Atómico DEX-a-DEX con Flash Swaps (`PancakeSwap V3` $\leftrightarrow$ `PancakeSwap V2` en vivo; `Thena` en banco de prueba).
3. **Bot #3 (`BOT_3_AUCTION_KICK_KEEPER`) — [Monitor On-Chain en Vivo]:** Monitoreo en vivo de deuda activa (`totalSupply()`) en **Lista DAO `lisUSD`** (`0x0782...41E5`) + evaluador matemático de bounties `kick()` en banco de prueba.
4. **Bot #4 (`BOT_4_VAULT_HARVEST_KEEPER`) — [Monitor On-Chain en Vivo]:** Monitoreo en vivo de balances LP (`balance()`) en bóvedas de **Beefy Finance BSC** + evaluador matemático de `harvest()` `callFee` en banco de prueba.
5. **Bot #5 (`BOT_5_PERP_ORDER_KEEPER`) — [Monitor On-Chain en Vivo]:** Monitoreo en vivo de `getMaxPrice(WETH)` en **GMX Vault Arbitrum** (`0x489e...97C4A`) + evaluador matemático de `executeOrder()` en banco de prueba.

> **Estado de Verificación:** Auditado con doble verificación independiente (GLM 5.3 línea a línea + Kimi K3 segunda opinión adjudicada).

---

## Arquitectura Real y Alcance Honesto (Fase 1 En Vivo + Fase 2 Compilada)

* **Fase 1 (Python Read-Only Engine — Activo en Vivo a Costo \$0):**
  * [`oracle_watcher.py`](./oracle_watcher.py): Lector JSON-RPC (`eth_call`, `eth_getLogs`, `eth_blockNumber`, `eth_gasPrice`) con bloqueo estricto de métodos de escritura (`WriteMethodForbiddenError`), validación *fail-closed* de oráculos Chainlink (`latestRoundData` `0xfeaf968c`), liquidez concentrada V3 (`slot0` `0x3850c7bd` / `liquidity` `0x1a686502`) y reservas V2 (`getReserves` `0x0902f1ac`).
  * [`position_indexer.py`](./position_indexer.py): Descubrimiento dinámico de prestatarios vía `eth_getLogs` (con `log_lookback_blocks` parametrizado por cadena) y decodificación de `getUserAccountData` (`0xbf92857c` en Aave V3 / Kinza — HF nativo) y `getAccountLiquidity` (`0x5ec88c79` en Venus Protocol — HF estimado derivado de `getAccountLiquidity`, con validación *fail-closed* `ORACLE_FEED_MISSING` si falta algún feed de `vToken`).
  * [`profit_simulator.py`](./profit_simulator.py) y [`commission_strategies.py`](./commission_strategies.py): Evaluación matemática determinista (con penalización *fail-closed* de `+150 bps` de *slippage* si falla la lectura de liquidez V3 en BNB) y **Cola Central de Prioridad (`rank_unified_opportunities`)** en un solo proceso de memoria RAM.
  * [`shadow_scanner.py`](./shadow_scanner.py): Escáner unificado en vivo (2 evaluadores económicos en vivo + 3 monitores on-chain en vivo) separado estrictamente del banco de prueba matemática (`SIMULACIÓN DE ESTRÉS`).
  * [`telegram_notifier.py`](./telegram_notifier.py) y [`dashboard_generator.py`](./dashboard_generator.py): Alertas con TLS verificado (`requests.post`), control anti-spam y generador de tablero HTML en vivo.
* **Fase 2 (Smart Contract Polimórfico en Solidity — Compilado, Pendiente de Despliegue Post-Auditoría):**
  * [`contracts/UniversalFlashExecutor.sol`](./contracts/UniversalFlashExecutor.sol):
    * Compilado realmente con `solc 0.8.20+commit.a1b79de6.Emscripten.clang` (`viaIR: true`, `0` errores, `8,323` bytes de runtime bytecode en [`contracts/artifacts/UniversalFlashExecutor.json`](./contracts/artifacts/UniversalFlashExecutor.json)).
    * **Autenticación de Callbacks (`_callbackContext`):** Bloquea llamadas directas o suplantadas a `pancakeV3FlashCallback` y `executeOperation` (`UnauthorizedCallbackCaller` / `UnauthorizedFlashInitiator`).
    * **Whitelist de Contratos (`whitelistedTargets`):** Impide inyección de llamadas arbitrarias.
    * **Cero Aprobaciones Residuales (`_safeApproveExact`):** Resetea el `allowance` a `0` antes y después de cada operación.
    * **Barrido Completo de Remanentes (`MEDIA-1`):** Barre tanto `collateralToken` residual como `borrowedToken` hacia `owner`.
    * **Invariante Atómica de Ganancia Neta (`AtomicRevertInsufficientNetProfit`):** Si la ganancia neta transferida a `owner` al final de la transacción es menor que `minNetProfit`, revierte toda la transacción con `\$0.00` de pérdida de capital y mantiene `\$0.00` de saldo custodial en el contrato.

---

## Comandos de Compilación y Verificación Determinista (Sin Red en Tests)

```bash
# 1. Compilar el Smart Contract (Fase 2) con solc 0.8.20
node compile_check.js

# 2. Ejecutar la suite determinista de pruebas offline (Fase 1 + Artefacto y Modelo Fase 2)
pytest -v

# 3. Ejecutar un ciclo en vivo del Motor 5-en-1 en Modo Sombra (RPC real BNB + Arbitrum)
python shadow_scanner.py --chains BNB ARBITRUM --cycles 1 --simulate-stress
```

