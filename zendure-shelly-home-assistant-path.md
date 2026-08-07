# Path 2: Home Assistant (Local Integrations)

## Overview
Run Home Assistant as the local hub connecting the Zendure 800 Plus and Shelly Pro 3EM. Both devices have fully local HA integrations (no cloud dependency), and HA can host the zero-feed-in / power-matching logic instead of a standalone Shelly script. More setup overhead than the direct-script approach, but more flexible (automations, dashboards, history, other integrations).

## Requirements
- A Home Assistant instance (already running, or would need to be set up — e.g. HA OS on a Raspberry Pi/mini PC, or HA Container/Supervised).
- Zendure 800 Plus and Shelly Pro 3EM both reachable on the local network.
- Shelly's native local HA integration (built-in, cloud-free) handles the Shelly side — no extra add-on needed for that part.

## Zendure Local Integration Options for HA

### Option A: Zendure-HA-zenSDK
- 100% local control via Zendure's zenSDK.
- Explicitly supports: Solarflow 2400 (AC/AC+/AC Pro), Solarflow 1600 AC+, **Solarflow 800 (Pro(2) or Plus)**, Solarflow 3000 Mix AC+, Solarflow 4000 Mix (AC+ or Pro) — i.e. directly covers your 800 Plus.
- Repo: https://github.com/Gielz1986/Zendure-HA-zenSDK

### Option B: home-assistant-zendure_local
- Custom HA component integrating with Zendure devices locally via REST API (ZenSDK).
- Tested with Zendure SolarFlow 800; may work with other models.
- Repo: https://github.com/TimSoethout/home-assistant-zendure_local

### Option C: solarflow (z-master42)
- Integrates Zendure products (Hub 1200, Hub 2000, Hyper 2000, Ace 1500) into HA via MQTT — note this covers different Zendure models than the 800 Plus; included for reference only, likely not the right fit for your device.
- Repo: https://github.com/z-master42/solarflow

### Background on Local vs Cloud MQTT
For Zendure devices generally, there are two MQTT paths: Cloud MQTT (via Zendure's official developer API) or Local MQTT/API, which is faster and works without internet. The zenSDK-based integrations (Options A/B) use the local REST API path rather than cloud MQTT.

## Non-HA Standalone Local Project (Reference)
- https://github.com/Utini2000/Zendure-Solarflow-Local-HomeAssistant — "Run Zendure SolarFlow 800 Pro devices locally without any cloud or internet access." Despite the repo name mentioning Home Assistant, worth checking whether it's an HA integration or a standalone script — review the repo directly to confirm scope before relying on it.

## Zendure Local API Reference
- Official zenSDK documentation (describes the local HTTP/REST endpoints used by the above integrations): https://github.com/Zendure/zenSDK

## Requirement Carried Over From Direct-Script Path
- Same caveat applies: the Zendure device must not be under a HEMS integration in the Zendure app, or the HEMS will overwrite externally-written setpoints (e.g. from an HA automation).

## Trade-offs vs. Direct Shelly Script (Path 1)
- **Pros**: HA gives you dashboards, historical graphs, notifications, and the ability to combine Zendure/Shelly data with other smart home logic; easier to debug (HA logs vs. Shelly script `print()` statements); community integrations are actively maintained repos rather than a single forum-posted script.
- **Cons**: Requires standing up and maintaining a full Home Assistant instance if you don't already run one; more moving parts (HA core + integration + your own automation for the zero-feed-in logic) compared to a single script living entirely on the Shelly.
