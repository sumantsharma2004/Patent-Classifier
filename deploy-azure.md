# Docker Deployment Guide for Azure VM

## Quick Deployment Steps

### 1. Provision Azure VM
```bash
# Recommended specs:
- Size: Standard B2s or larger (2 vCPUs, 4 GB RAM)
- OS: Ubuntu 22.04 LTS
- Open ports: 22 (SSH), 80 (HTTP), 443 (HTTPS)
```

### 2. Connect to VM
```bash
ssh azureuser@<your-vm-ip>
```

### 3. Install Docker & Git
```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Install Git
sudo apt install git -y

# Install Docker
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh

# Add user to docker group
sudo usermod -aG docker $USER

# Install Docker Compose
sudo curl -L "https://github.com/docker/compose/releases/latest/download/docker-compose-$(uname -s)-$(uname -m)" -o /usr/local/bin/docker-compose
sudo chmod +x /usr/local/bin/docker-compose

# Log out and back in for group changes
exit
# SSH back in
ssh azureuser@<your-vm-ip>

# Verify installation
git --version
docker --version
docker-compose --version
```

### 4. Deploy Application
```bash
# Create application directory
mkdir -p ~/ieb-classifier
cd ~/ieb-classifier

# Upload files (from local machine)
# Option 1: Use SCP
scp -r /path/to/local/ieb-classifier/* azureuser@<vm-ip>:~/ieb-classifier/

# Option 2: Use Git
git clone <your-repo-url> .

# Build and start container
docker-compose up -d

# Check status
docker-compose ps

# View logs
docker-compose logs -f
```

### 6. Configure Azure Firewall
```bash
# In Azure Portal, add NSG inbound rules:
# - Port 8501 (Streamlit) - Source: Your IP or Any
# - Port 22 (SSH) - Source: Your IP only
```

### 5. Access Application
```
http://<your-vm-ip>:8501
```

**Credentials:** Enter Azure OpenAI credentials directly in the web UI:
- Upload `.env` file in the app sidebar, OR
- Enter credentials manually in the sidebar fields

## Optional: Setup Nginx Reverse Proxy (for custom domain/SSL)

### Install Nginx
```bash
sudo apt update
sudo apt install nginx -y
```

### Configure Nginx
```bash
sudo nano /etc/nginx/sites-available/ieb-classifier
```

Add:
```nginx
server {
    listen 80;
    server_name your-domain.com;  # or use VM IP

    location / {
        proxy_pass http://localhost:8501;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 86400;
    }
}
```

```bash
# Enable site
sudo ln -s /etc/nginx/sites-available/ieb-classifier /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl restart nginx

# Update Azure NSG to allow port 80
```

Access at: `http://your-domain.com` or `http://<vm-ip>`

### Optional: Add SSL Certificate
```bash
sudo apt install certbot python3-certbot-nginx -y
sudo certbot --nginx -d your-domain.com

# Update Azure NSG to allow port 443
```

Access at: `https://your-domain.com`

## Management Commands

### View Logs
```bash
docker-compose logs -f
```

### Restart Application
```bash
docker-compose restart
```

### Stop Application
```bash
docker-compose down
```

### Update Application
```bash
# Pull latest code
git pull

# Rebuild and restart
docker-compose down
docker-compose up -d --build
```

### View Container Status
```bash
docker-compose ps
docker stats
```

### Access Container Shell
```bash
docker-compose exec ieb-classifier bash
```

## Auto-start on VM Reboot

### Enable Docker to start on boot
```bash
sudo systemctl enable docker
```

### Configure restart policy (already in docker-compose.yml)
The `restart: unless-stopped` policy ensures the container restarts automatically.

## Backup & Restore

### Backup Docker image
```bash
docker save ieb-classifier-ieb-classifier > ieb-classifier-backup.tar
```

### Restore Docker image
```bash
docker load < ieb-classifier-backup.tar
```

## Monitoring

### Check container health
```bash
docker inspect --format='{{.State.Health.Status}}' ieb-classifier
```

### Monitor resource usage
```bash
docker stats ieb-classifier
```

### View container logs
```bash
docker logs ieb-classifier --tail 100 -f
```

## Troubleshooting

### Container won't start
```bash
# Check logs
docker-compose logs

# Check if port is already in use
sudo lsof -i :8501

# Remove container and rebuild
docker-compose down
docker-compose up -d --build
```

### Application not accessible
```bash
# Check container status
docker-compose ps

# Check if port is exposed
docker port ieb-classifier

# Check Azure NSG rules
# Ensure port 8501 (or 80/443 if using nginx) is open
```

## Security Best Practices

1. **Credentials in UI**: Enter Azure OpenAI credentials via the web interface
2. **Use SSL/HTTPS** in production (via nginx + certbot)
3. **Restrict Azure NSG** to specific IPs
4. **Regular updates**: `docker-compose pull && docker-compose up -d`
5. **Monitor logs** for suspicious activity
6. **Use secrets management** for production (Azure Key Vault)

## Cost Optimization

- Use **Azure VM Auto-shutdown** in portal
- Use **B-series burstable VMs** for cost savings
- Consider **Azure Container Instances** for serverless option

## Production Checklist

- [ ] Azure NSG configured (ports 22, 80, 443, 8501)
- [ ] Docker and Docker Compose installed
- [ ] Application deployed and running
- [ ] Nginx reverse proxy configured (optional)
- [ ] SSL certificate installed (optional)
- [ ] Auto-restart enabled
- [ ] Monitoring setup
- [ ] Backup strategy defined
