packer {
  required_plugins {
    vmware = {
      version = ">= 1.1.0"
      source  = "github.com/hashicorp/vmware"
    }
  }
}

variable "debian_iso_url" {
  type        = string
  description = "Debian 13 amd64 netinst ISO URL"
}

variable "debian_iso_checksum" {
  type        = string
  description = "sha256:<hex> checksum for Debian 13 ISO"
}

variable "mihomo_version" {
  type    = string
  default = "v1.19.19"
}

variable "mihomo_sha256" {
  type        = string
  description = "SHA-256 of mihomo-linux-amd64-<version>.gz"
}

variable "output_dir" {
  type    = string
  default = "output-debian13-amd64"
}

source "vmware-iso" "debian13" {
  iso_url          = var.debian_iso_url
  iso_checksum     = var.debian_iso_checksum
  guest_os_type    = "debian12-64"
  cpus             = 2
  memory           = 2048
  disk_size        = 24576
  disk_type_id     = "0"
  headless         = true
  http_directory   = "http"
  boot_wait        = "5s"
  boot_command = [
    "<esc><wait>",
    "auto url=http://{{ .HTTPIP }}:{{ .HTTPPort }}/preseed.cfg ",
    "hostname=debian domain=local ",
    "interface=auto ",
    "DEBCONF_DEBUG=5 <enter>"
  ]
  ssh_username     = "root"
  ssh_password     = "cchouse-build-temp"
  ssh_timeout      = "25m"
  shutdown_command = "systemctl poweroff"
  output_directory = var.output_dir
  vm_name          = "cchouse-debian13-amd64"
  # 构建期保持网卡连通以便 preseed/apt；导出后通过 vmx_data_post 物理断网并禁用共享文件夹
  vmx_data_post = {
    "ethernet0.startConnected"     = "FALSE"
    "isolation.tools.hgfs.disable" = "TRUE"
    "isolation.tools.dnd.disable"  = "TRUE"
  }
}

build {
  sources = ["source.vmware-iso.debian13"]

  provisioner "shell" {
    inline = ["mkdir -p /tmp/cchouse-guest-linux"]
  }

  provisioner "file" {
    source      = "../"
    destination = "/tmp/cchouse-guest-linux"
  }

  provisioner "shell" {
    environment_vars = [
      "MIHOMO_VERSION=${var.mihomo_version}",
      "MIHOMO_SHA256=${var.mihomo_sha256}",
    ]
    inline = [
      "bash /tmp/cchouse-guest-linux/image-builder/build-debian13-golden.sh",
      "rm -rf /tmp/cchouse-guest-linux"
    ]
  }
}
