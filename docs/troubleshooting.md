[← README](https://github.com/dougiteixeira/proxmoxve#readme) · [Entities](entities.md) · [Actions](actions.md) · [Hardware sensors](hardware-sensors.md) · [Permissions](permissions.md) · [Behaviour](behaviour.md) · [Troubleshooting](troubleshooting.md) · [Compared with core](compared-to-core.md)

# Troubleshooting

## Debug logging

To enable debug logging for a specific integration, follow these steps:

* Go to Settings > Devices & services.
* Select the integration card to open the detail page of the integration for which you want to enable debug logging.
* On the left side of the integration detail page, select Enable Debug Logging.

<details><summary>If you prefer, you can configure debugging through the `configuration.yaml` file</summary>

To enable debug for Proxmox VE integration, add following to your `configuration.yaml`:
```yaml
logger:
  default: info
  logs:
    custom_components.proxmoxve: debug
```
</details>

## "Could not verify the SSL certificate"

Proxmox signs every node's certificate with the cluster's own CA, which no public list knows. Three ways out, from safest to least safe:

1. Install the cluster's CA — `/etc/pve/pve-root-ca.pem` on any node — in Home Assistant's operating system store. On Home Assistant OS the [Additional CA](https://github.com/Athozs/hass-additional-ca) integration does this; the integration trusts that store alongside the public list.
2. Put the same file somewhere Home Assistant can read it, for example `/config/pve-root-ca.pem`, and enter that path as **CA bundle for a private CA** on the host form (also in the options under *Change host authentication information*). The file is read when the integration starts; a path that cannot be read stops setup with a message rather than retrying.
3. Turn **Verify SSL certificate** off. The connection is still encrypted, but the node's identity is not checked.

The CA Proxmox generates at install time carries no keyUsage extension, which Python's strict certificate checks (on by default since 3.13) refuse regardless of trust. The integration turns those profile checks off for its own connections; the chain and the hostname are still verified.

With a fallback to another cluster node (see [Behaviour](behaviour.md)), that node's certificate has to be valid for the address it is reached on as well.

## Diagnostics

The integration supports Home Assistant's standard diagnostics download (Settings > Devices & services > Proxmox VE > ⋮ > Download diagnostics), useful for attaching to bug reports. It includes the config entry's settings (credentials redacted) and a snapshot of the last data polled by every active coordinator (nodes, VMs/CTs, storage, disks, ZFS, tasks, updates, and — if configured — the HA-managed resource list and cluster HA status). Node, VM/CT, and storage names are not redacted since they're the point of a diagnostics dump; review the file before sharing it publicly if that's a concern for your setup.

## Screenshots

Here are some screenshots of the integration

<details><summary>Node</summary>

![image](https://github.com/dougiteixeira/proxmoxve/assets/31328123/e371b34e-0449-499f-878b-b5baacee8a5e)

</details>

<details><summary>VM (QEMU)</summary>
 
![image](https://github.com/dougiteixeira/proxmoxve/assets/31328123/8213b877-8b23-4c4a-917b-04f27bb3a886)
 
</details>

<details><summary>Storage</summary>
 
![image](https://github.com/dougiteixeira/proxmoxve/assets/31328123/fb290802-95d7-4dcc-8538-d31636a2f6f8)
 
</details>

<details><summary>Physical disks</summary>
 
![image](https://github.com/dougiteixeira/proxmoxve/assets/31328123/f6174806-0ba8-4f60-ada7-cf5f29a1f629)
 
</details>
