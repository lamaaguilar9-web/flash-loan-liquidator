# Sentinel Fleet — Motor Único 5-en-1 de Comisiones On-Chain (`flash-loan-liquidator`)

Motor unificado de detección en Modo Sombra (**Fase 1 — Python**) y contrato inteligente ejecutor polimórfico atómico (**Fase 2 — Solidity `0.8.20`**) para las 5 estrategias de comisiones on-chain en **BNB Chain** y **Arbitrum One**:

1. **Bot #1 (`BOT_1_FLASH_LIQUIDATION`):** Liquidaciones con Flash Loans en Venus Protocol, Kinza Finance y Aave V3 (bono `5% a 10%`).
2. **Bot #2 (`BOT_2_DEX_FLASH_ARBITRAGE`):** Arbitraje Atómico DEX-a-DEX con Flash Swaps (`PancakeSwap V3` $\leftrightarrow$ `PancakeSwap V2` / `Thena`).
3. **Bot #3 (`BOT_3_AUCTION_KICK_KEEPER`):** Bounties de inicio de subastas holandesas (`kick()` en **Lista DAO `lisUSD`** / Liquity forks).
4. **Bot #4 (`BOT_4_VAULT_HARVEST_KEEPER`):** Cosechador de bóvedas de rendimiento (`harvest()` en **Beefy Finance**) cobrando el `callFee`.
5. **Bot #5 (`BOT_5_PERP_ORDER_KEEPER`):** Keeper de órdenes límite, *stop-loss* y liquidaciones de perpetuos (`executeOrder()` en **ApolloX / GMX**).

> **Estándar de Verificación:** Código auditado internamente con doble verificación independiente — un auditor línea a línea y una segunda opinión por muestreo, con la evidencia pública en el repositorio.

---

## Arquitectura Fase 1 (Modo Sombra) y Fase 2 (Contrato Atómico)

* **Fase 1 (Python Read-Only Engine):**
  * [`oracle_watcher.py`](./oracle_watcher.py): Lector `eth_call` de oráculos Chainlink (`latestRoundData` `0xfeaf968c`) y liquidez concentrada (`slot0` / `liquidity`) con candados *fail-closed*.
  * [`position_indexer.py`](./position_indexer.py): Descubrimiento dinámico de prestatarios vía `eth_getLogs` y decodificación de `getUserAccountData` (`0xbf92857c`) y `getAccountLiquidity` (`0x5ec88c79`).
  * [`profit_simulator.py`](./profit_simulator.py) y [`commission_strategies.py`](./commission_strategies.py): Evaluación matemática determinista de los 5 bots y **Cola Central de Prioridad (`rank_unified_opportunities`)** en un solo proceso de memoria RAM.
* **Fase 2 (Smart Contract Polimórfico en Solidity):**
  * [`contracts/UniversalFlashExecutor.sol`](./contracts/UniversalFlashExecutor.sol):
    * Compilado con `solc 0.8.20+commit.a1b79de6` (`viaIR: true`, `0` errores, `8,130` bytes).
    * **Autenticación de Callbacks (`_callbackContext`):** Bloquea llamadas directas o suplantadas a `pancakeV3FlashCallback` y `executeOperation` (`UnauthorizedCallbackCaller` / `UnauthorizedFlashInitiator`).
    * **Whitelist de Contratos (`whitelistedTargets`):** Impide inyección de llamadas arbitrarias.
    * **Cero Aprobaciones Residuales (`_safeApproveExact`):** Resetea el `allowance` a `0` antes y después de cada operación.
    * **Invariante Atómica de Ganancia Neta (`AtomicRevertInsufficientNetProfit`):** Si la ganancia neta transferida a `owner` al final de la transacción es menor que `minNetProfit`, revierte toda la transacción con `$0.00` de pérdida de capital y mantiene `$0.00` de saldo custodial en el contrato.

---

## Comandos de Compilación y Auditoría

```bash
# 1. Compilar el Smart Contract (Fase 2) con solc 0.8.20
node compile_check.js

# 2. Ejecutar la suite completa de 23 pruebas (Fase 1 + Fase 2)
pytest -v

# 3. Ejecutar un ciclo en vivo del Motor 5-en-1 en Modo Sombra
python shadow_scanner.py --chains BNB ARBITRUM --cycles 1 --simulate-stress
```
