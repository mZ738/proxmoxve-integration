# Proxmox VE Custom Integration for Home Assistant
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://github.com/user-attachments/assets/5b5a8c5b-885b-4233-a858-2e78b97d8c74">
  <img src="https://github.com/dougiteixeira/proxmoxve/assets/31328123/dfec7426-852d-41ea-b6c1-9bfd8cd1e8a8">
</picture>


[Proxmox VE](https://www.proxmox.com/en/) is an open-source server virtualization environment. This integration allows you to poll various data and controls from your instance.

This integration started as improvements to the [Home Assistant core's Proxmox VE integration](https://www.home-assistant.io/integrations/proxmoxve/), but I'm new to programming and couldn't meet all of the core's code requirements. So I decided to keep it as a custom integration. Therefore, when installing this, the core integration will be replaced.

After configuring this integration, the following information is available:

 - Binary sensor entities with the status of node and selected virtual machines/containers.
 - Sensor entities of the selected node and virtual machines/containers. Some sensors are created disabled by default, you can enable them by accessing the entity's configuration.
 - **Failed task monitoring sensors** that track failed tasks from the last 24 hours on selected nodes, showing the count of failures and details about recent failed tasks.
 - Entities button to control selected virtual machines/containers (see about Proxmox user permissions below). By default, the entities buttons to control virtual machines/containers are created disabled, [see how to enable them here](#disabled-entities).

### Failed Task Monitoring

The integration provides sensors that monitor failed tasks on your Proxmox nodes over the last 24 hours. These sensors offer:

- **Failed Task Count**: Shows the number of failed tasks per node
- **Recent Failure Details**: Displays information about recent failed tasks including:
  - Task type (backup, migration, etc.)
  - Start and end timestamps (in your local timezone)
  - Task status
- **Configurable**: Can be enabled or disabled during integration setup or via integration options
- **Automatic Updates**: Refreshes every 5 minutes to provide up-to-date information

The failed task sensors help you monitor the health of your Proxmox operations and quickly identify when automated tasks encounter issues.

### Hardware Sensors

The integration automatically discovers and exposes hardware temperature, voltage, power, current, and fan speed sensors from Proxmox VE hosts via `lm-sensors`.

#### Prerequisites

On each Proxmox VE host, install `lm-sensors` and [PVE-mods](https://github.com/Meliox/PVE-mods), either variant:

- **v2 (`node_info`, current)** — installed via its Debian package/configure wizard. Exposes sensor data under a `PveMod_JsonSensorInfo` field (temperature only; its separate GPU/UPS/system-info fields aren't read by this integration). See the PVE-mods README for install instructions.
- **Legacy script (`pve-mod-gui-sensors.sh`)** — still supported, exposes a `sensorsOutput` field:
  ```bash
  apt-get install lm-sensors
  wget https://raw.githubusercontent.com/Meliox/PVE-mods/main/legacy-scripts/pve-mod-gui-sensors.sh
  bash pve-mod-gui-sensors.sh install
  ```

Both are auto-detected — whichever one is installed and enabled for temperature sensors is used, no configuration needed on the integration side. Compatible with Proxmox VE 9.0-9.2 per the PVE-mods README; check there for current install instructions if paths change again.

This modifies the Proxmox VE API to inject `sensors -j` output into the `GET /nodes/{node}/status` response. No additional API calls are made by the integration.

#### Supported Hardware

| Chip / Driver | Device Type | Examples |
|---------------|-------------|----------|
| `k10temp`, `k8temp`, `coretemp`, `peci-cputemp` | CPU | Tctl, Tdie, Package temperature |
| `amdgpu`, `i915`, `nvidia_gpu` | GPU | Core voltage, hotspot temperature, power, clock |
| `nvme`, `drivetemp` | Storage | NVMe/Drive temperature |
| `jc42`, `spd5118`, `sodimm` | Memory | DIMM temperature |
| `nct6775`, `it87`, `w83627` | Motherboard | System/CPU/Aux temperature |
| `mlx5`, `igb`, `ixgbe` | NIC | NIC temperature, power |
| `pmbus`, `corsair`, `lm25066` | PSU | Power supply temperature, power |
| `emc2305`, `pwm-fan`, `max31785` | Cooling | Fan speed (RPM) |

#### Auto-classification

Each sensor is automatically classified:

- **Names** mapped from known labels (e.g. `Tctl` → `CPU control temperature`, `edge` → `GPU hotspot`)
- **Units** inferred from sensor name patterns (temperature in °C, voltage in V, power in W, frequency in MHz, current in A, fan speed in RPM)
- **Device classes** set accordingly (`temperature`, `voltage`, `power`, `frequency`, `current`)
- **Icons** assigned per device type

Sensors are created under the corresponding Node device in Home Assistant and are marked as `diagnostic`.

### Guest File Content Sensor

For QEMU virtual machines with the [QEMU Guest Agent](https://pve.proxmox.com/wiki/Qemu-guest-agent) installed and running, you can configure an absolute file path (in the integration options) that will be read from inside each tracked VM and exposed as a sensor.

- Configured once for all tracked QEMU VMs, via the integration options (`Guest file path to monitor`). Leave empty to disable (default).
- Only VMs where the file can actually be read (guest agent running, file exists and is accessible) get the sensor; it is silently skipped otherwise.
- Content is capped at 4 KiB per read; the sensor state is further truncated to 255 characters (Home Assistant's state length limit), with the full (capped) content available as the `guest_file_content` attribute.
- QEMU only — LXC containers have no equivalent guest-agent file-read API.

### Cluster HA Administration (Advanced, Optional)

Two features let you interact with the Proxmox HA (High Availability) stack, gated behind a **separate, optional** set of credentials:

- **Arm HA / Disarm HA buttons**: cluster-wide equivalents of `ha-manager crm-command arm-ha` / `disarm-ha`, letting you pause HA fencing for planned maintenance (e.g. before/after a node reboot script) and resume it afterwards. Disabled by default even once configured — enable them explicitly like other advanced entities. Disarm always uses `resource-mode=freeze` (HA services stay locked in their current state, no automatic action) rather than `ignore` (which fully suspends HA tracking and allows manual guest management) — the safer of the two, but it means guests aren't freely manageable outside of HA while disarmed. Arm HA resumes normal monitoring from whatever the actual state is at that point; it does not roll anything back.
- **"HA managed" sensor**: a per-VM/CT binary sensor showing whether that guest is currently a Proxmox HA resource.

> [!CAUTION]
> These need **`Sys.Console`** (arm/disarm) and **`Sys.Audit`** (HA resource list) on the Proxmox **root path (`/`)** — cluster-wide permissions, well beyond the scoped, per-node/per-VM permissions the rest of this integration recommends. `Sys.Console` in particular is normally associated with shell/console access. Only configure this if you understand and accept that risk.

Because of that, this uses a **separate user or API token** from the main integration credentials, configured via the integration options (`Optional: cluster HA administration (advanced)`). Leave every field empty (the default) to keep these features disabled — the rest of the integration is unaffected either way. A failure to authenticate with these optional credentials only disables these features; it does not break the rest of the integration.

Only relevant if you run a Proxmox **cluster with HA-manager configured** — on a standalone node there are no HA resources to arm/disarm or report on.

> [!IMPORTANT]  
> See the section on Proxmox user permissions [here](#proxmox-permissions).

## Install

### Installation via HACS

Have [HACS](https://hacs.xyz/) installed, this will allow you to update easily.

* Adding Proxmox VE to HACS can be using this button:

[![image](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=dougiteixeira&repository=proxmoxve&category=integration)

> [!NOTE]
> If the button above doesn't work, add `https://github.com/dougiteixeira/proxmoxve` as a custom repository of type Integration in HACS.

* Click Install on the `Proxmox VE` integration.
* Restart the Home Assistant.

<details><summary>Manual installation</summary>
 
* Copy `proxmoxve`  folder from [latest release](https://github.com/dougiteixeira/proxmoxve/releases/latest) to [`custom_components` folder](https://developers.home-assistant.io/docs/creating_integration_file_structure/#where-home-assistant-looks-for-integrations) in your config directory.
* Restart the Home Assistant.
</details>

## Configuration

Adding Proxmox VE to your Home Assistant instance can be done via the UI using this button:

[![image](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start?domain=proxmoxve)

> [!TIP]
> It is recommended to use token-based authentication for greater integration stability.
> 
> In your Home Assistant configuration, enter the value defined in `Token ID` in the `Token name` field and enter the secret token value in the password field.

> [!NOTE]
> To use user-based authentication only, you must leave the `Token name` field empty in the configuration flow.

> [!IMPORTANT]
> It is important to correctly define the user's realm (`pam`, `pve` or other).
>
> You can check this in Proxmox under Datacenter > Permissions > Users > Realm column

<details><summary>Manual Configuration</summary>

If the button above doesn't work, you can also perform the following steps manually:

* Navigate to your Home Assistant instance.
* In the sidebar, click Settings.
* From the Setup menu, select: Devices & Services.
* In the lower right corner, click the Add integration button.
* In the list, search and select `Proxmox VE`.
* Follow the on-screen instructions to complete the setup.
</details>
 
## Debugging

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

### Diagnostics

The integration supports Home Assistant's standard diagnostics download (Settings > Devices & services > Proxmox VE > ⋮ > Download diagnostics), useful for attaching to bug reports. It includes the config entry's settings (credentials redacted) and a snapshot of the last data polled by every active coordinator (nodes, VMs/CTs, storage, disks, ZFS, tasks, updates, and — if configured — the HA-managed resource list). Node, VM/CT, and storage names are not redacted since they're the point of a diagnostics dump; review the file before sharing it publicly if that's a concern for your setup.

## Example screenshot:
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

## Proxmox Permissions

> [!IMPORTANT]  
> It is necessary to reload the integration after changing user/token permissions in Proxmox.

To be able to obtain each type of integration information, the user used to connect must have the corresponding privilege.

It is not necessary to include all of the permission roles below, this will depend on your use of the integration.

The integration will create a repair for each resource that is exposed in the integration configuration but is not accessible by the user, indicating the path and privilege necessary to access it.

When executing a command, if the user does not have the necessary permission, a repair will be created indicating the path and privilege necessary to execute it.

> [!CAUTION]
> The permissions suggested in this documentation and in the created repairs are informative, the responsibility for assessing the risks involved in assigning permissions to the user is the sole responsibility of the user.

### Suggestion for creating permission roles for use with integration

Below is a summary of the permissions for each integration feature. I suggest you create the roles below to make it easier to assign only the necessary permissions to the user.

|Purpose of Permission|Access Type|Role (name suggestion)|Privilegies|
|---|---|---|---|
|Get data from nodes, VM, CT and storages|Read only|HomeAssistant.Audit|VM.Audit, Sys.Audit and Datastore.Audit|
|Perform commands on the node (shutdown, restart, start all, shutdown all)|Management permission|HomeAssistant.NodePowerMgmt|Sys.PowerMgmt|
|Get information about available package updates to display on sensors (integration does not trigger the update)|Management permission|HomeAssistant.Update|Sys.Modify|
|Perform commands on VM/CT (start, shutdown, restart, suspend, resume and hibernate)|Management permission|HomeAssistant.VMPowerMgmt|VM.PowerMgmt|
|**(Optional, separate user/token — see [Cluster HA Administration](#cluster-ha-administration-advanced-optional))** Arm/Disarm HA and read the HA-managed resource list, root-scoped (`/`)|Cluster-wide management permission|HomeAssistant.ClusterHA|Sys.Console, Sys.Audit|

### Create Home Assistant Group

Before creating the user, we need to create a group for the user.
Privileges can be either applied to Groups or Roles.

1. Click `Datacenter`
2. Open `Permissions` and click `Groups`
3. Click the `Create` button above all the existing groups
4. Name the new group (e.g., `HomeAssistant`)
5. Click `Create`

### Add Group Permissions to all Assets

1. Click `Datacenter`
2. Click `Permissions`
3. Open `Add` and click `Group Permission`
4. Select the path of the resource you want to authorize the user to access. To enable all features select `/`
5. Select your Home Assistant group (`HomeAssistant`)
6. Select the role according to the table above (you must add a permission for each role in the table).
7. Make sure `Propagate` is checked

### Create Home Assistant User

Creating a dedicated user for Home Assistant, limited to only to the access just created is the most secure method. These instructions use the `pve` realm for the user. This allows a connection, but ensures that the user is not authenticated for SSH connections.

1. Click `Datacenter`
2. Open `Permissions` and click `Users`
3. Click `Add`
4. Enter a username (e.g.,` homeassistant`)
5. Set the realm to "Proxmox VE authentication server"
6. Enter a secure password (it can be complex as you will only need to copy/paste it into your Home Assistant configuration)
7. Select the group just created earlier (`HomeAssistant`) to grant access to Proxmox
8. Ensure `Enabled` is checked and `Expire` is set to "never"
9. Click `Add`

### Create token for user (recommended)

Creating a dedicated user token for Home Assistant, limited only to the newly created access, is the most recommended method.

1. Click `Datacenter`
2. Open `Permissions` and click `API Tokens`
3. Click `Add`
4. Select the user linked to the token
5. Enter a name for the token in the `Token ID` field (e.g.,` homeassistant`)
6. Uncheck the `Privilege Separation` option to unlink user permissions (in this case, unique permissions must be configured for the token)
7. Select the Never option in the `Expire` field
8. Ensure `Enabled` is checked and `Expire` is set to "never"
9. Click `Add`
10. Copy the secret token value

> [!WARNING]
> After closing the popup it is not possible to recover this value, if you lose the token value you must create a new token.

> [!TIP]
> In your Home Assistant configuration, enter the value defined in `Token ID` in the `Token name` field.

## Disabled entities

Some entities are disabled by default (including control buttons), see below how to enable them.

 <details><summary>A step by step to enable entities</summary>
  
   1) Go to the page for the device you want to enable the button (or sensor).

      ![image](https://github.com/dougiteixeira/proxmoxve/assets/31328123/4e3f9b7d-e935-4fc5-bdd3-3329ef9b90a8)
   
   2) Click +x entities not show

      ![image](https://github.com/dougiteixeira/proxmoxve/assets/31328123/0240d2ed-efac-4c59-9def-e721a44dde90)
   
   3) Click on the entity you want to enable and click on settings (on the gear icon):

      ![image](https://github.com/dougiteixeira/proxmoxve/assets/31328123/e1bd2fb2-6fb5-4919-88c1-8056b7435f87)
   
   4) Click the Enable button at the top of the dialog:

      ![image](https://github.com/dougiteixeira/proxmoxve/assets/31328123/1a8205e4-a779-4a01-922d-5d147e8e5766)
   
   5) Wait a while (approximately 30 seconds) for the entity to be enabled. If you don't want to wait, just reload the configuration entry on the integration page.

      ![image](https://github.com/dougiteixeira/proxmoxve/assets/31328123/33edd547-8c55-44eb-b0b9-5036317bf077)
   
   For the entity to appear enabled on the device page, it may be necessary to refresh the page.
   </details>

> [!NOTE]
> The Wake on LAN button only works if the configured node is in a cluster of two or more nodes. If you want to use WOL on a single Node, use the official `Wake-On-Lan` integration.

## Translations
[![Crowdin](https://badges.crowdin.net/proxmoxve-homeassistant/localized.svg)](https://crowdin.com/project/proxmoxve-homeassistant)

You can help by adding missing translations when you are a native speaker. Or add a complete new language when there is no language file available.

Proxmox VE Custom Integration uses [Crowdin](https://crowdin.com) to make contributing easy.

### Changing or adding to existing language

First register and join the translation project:
* If you don’t have a Crowdin account yet, create one at https://crowdin.com
* Go to the [Proxmox VE Custom Integration for Home Assistant project page](https://crowdin.com/project/proxmoxve-homeassistant)
* Click Join.

Next translate a string:
* Select the language you want to contribute to from the dashboard.
* Click Translate All.
* Find the string you want to edit, missing translation are marked red.
* Fill in or modify the translation and click Save.
* Repeat for other translations.

### Adding a new language

[Create an Issue](https://github.com/dougiteixeira/proxmoxve/issues/new?template=new_language_request.yml&title=New+language) requesting a new language. We will do the necessary work to add the new translation to the integration and Crowdin site, when it's ready for you to contribute we'll comment on the issue you raised.
