# Let's Encrypt certificate, by hand (manual DNS challenge)

Inbound 443 only accepts Lark and Zoho source IPs, so Let's Encrypt cannot connect in to verify the
domain. The DNS challenge verifies it with a TXT record instead. Nothing in this folder is committed
(see .gitignore); the files live only on the server.

## Issue (and re-issue every ~80 days; the certificate lasts 90)

On the server, in /root/alert-ir-bot, replace HOST with the real hostname:

```bash
docker run -it --rm -v "$PWD/deploy/certs:/etc/letsencrypt" certbot/certbot certonly \
  --manual --preferred-challenges dns --agree-tos --no-eff-email \
  -m YOUR_EMAIL -d HOST
```

certbot prints something like:

```
Please deploy a DNS TXT record under the name:
_acme-challenge.HOST.
with the following value:
Xy12...
Press Enter to Continue
```

Do NOT press Enter yet.

1. Namecheap → Domain List → your domain → Manage → Advanced DNS → Add New Record:
   Type `TXT Record`, Host `_acme-challenge.alert-bot` (only the part before your domain),
   Value = the string certbot printed, TTL `1 min`. Save.
2. Wait about a minute, then check from your Mac: `dig +short TXT _acme-challenge.HOST`
   must print the value.
3. Press Enter in certbot. It writes
   `deploy/certs/live/HOST/fullchain.pem` and `privkey.pem`.
4. `docker compose restart caddy` (first time: `docker compose up -d`). Then
   `curl -s https://HOST/health`.
5. The TXT record can be deleted afterwards; a new value is needed next time anyway.

Expiry check: `openssl x509 -enddate -noout -in deploy/certs/live/HOST/fullchain.pem`.
Put a calendar reminder 80 days after each issue.
