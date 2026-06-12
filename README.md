# NetPrisma Fan Dashboard

Local dashboard and REST API for OmniBreeze / Landbook tower fans using the stock Wi-Fi module. Docker-based, Home Assistant friendly, with automatic device discovery.

This project runs locally in Docker and provides:

- A local web dashboard
- REST API endpoints
- Landbook / NetPrisma email/password login
- Automatic UID detection
- Automatic family ID detection
- Automatic product key detection
- Power, speed, oscillation, and sound controls
- Temperature and device status display
- Basic-auth protection
## Screenshot of the Dashboard
![Dashboard screenshot](assets/dashboard.png)
## Important notes

This project is unofficial and is not affiliated with NetPrisma, Landbook, Quectel, or OmniBreeze.

Do not expose this dashboard directly to the internet. Run it on your LAN, VPN, or behind a trusted reverse proxy with proper authentication.

Do not commit your .env file.

## Requirements

- Docker
- Docker Compose
- A Landbook / NetPrisma account
- At least one supported fan already added in the official app

## Setup

Clone the repository:

    git clone https://github.com/YOUR_USERNAME/netprisma-fan-dashboard.git
    cd netprisma-fan-dashboard

Create your private environment file:

    cp .env.example .env
    nano .env

Example .env:

    DASHBOARD_USER=admin
    DASHBOARD_PASS=admin

    NETPRISMA_EMAIL=your-landbook-email@example.com
    NETPRISMA_PASSWORD=your-landbook-password

    NETPRISMA_USER_DOMAIN=U.SP.8589934603
    NETPRISMA_USER_DOMAIN_SECRET=pUTp5goB1bLinprRQMmK3EPiiuPiGrJtKUNptWRXVmP

    #NETPRISMA_UID=
    #NETPRISMA_FID=
    #NETPRISMA_PRODUCT_KEY=

Start the container:

    docker compose up -d --build

Open the dashboard:

    http://YOUR_SERVER_IP:8099

## API

Get current fan state:

    curl -u admin:admin http://YOUR_SERVER_IP:8099/api/state

Send a command:

    curl -u admin:admin\
      -H "Content-Type: application/json" \
      -d '{"device_key":"DEVICE_KEY","action":"on"}' \
      http://YOUR_SERVER_IP:8099/api/command

Supported actions:

- on
- off
- speed:1
- speed:2
- speed:3
- osc_on
- osc_off
- sound_on
- sound_off

Turn all fans on:

    curl -u admin:admin \
      -H "Content-Type: application/json" \
      -d '{"action":"on"}' \
      http://YOUR_SERVER_IP:8099/api/all

Turn all fans off:

    curl -u admin:admin \
      -H "Content-Type: application/json" \
      -d '{"action":"off"}' \
      http://YOUR_SERVER_IP:8099/api/all

## Security

Keep these private:

- .env
- your Landbook / NetPrisma email
- your Landbook / NetPrisma password
- Bearer tokens
- refresh tokens
- real device keys

## Disclaimer

Use at your own risk. This project depends on private/unofficial cloud API behavior that may change without notice.
