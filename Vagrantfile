require "fileutils"

FileUtils.mkdir_p("logs")
FileUtils.mkdir_p("shared_folder")
FileUtils.mkdir_p("shared_folder/logs")

Vagrant.configure("2") do |config|

  config.vm.box = "bento/ubuntu-22.04"
  config.vm.boot_timeout = 600 # this host runs 4 VMs at once and can be slow to boot under load

  nodes = {
    "k8s-master" => {
      ip: "192.168.56.10",
      memory: 4096,
      cpus: 2
    },
    "k8s-worker1" => {
      ip: "192.168.56.11",
      memory: 2096,
      cpus: 2
    },
    "buildserver" => {
      ip: "192.168.56.20",
      memory: 1536, # docker + ansible + a JVM (openjdk) + a registry container need more than 512MB
      cpus: 1
    },
    "monitoring" => {
      ip: "192.168.56.21",
      memory: 1536, # prometheus + grafana need more than 512MB
      cpus: 1
    },
    "llm" => {
      ip: "192.168.56.30",
      memory: 8192, # qwen2.5:3b-instruct (Q4, ~2GB) + Ollama + KV-cache headroom -
                    # started at qwen3:4b, moved off it after real testing found
                    # its thinking mode unreliable, see ai-agent/README.md
      cpus: 4
    }
  }

  nodes.each do |name, cfg|

    config.vm.define name do |node|

      node.vm.hostname = name

      node.vm.network "private_network",
        ip: cfg[:ip]

      node.vm.synced_folder "./shared_folder",
                            "/shared_folder",
                            create: true

      node.vm.provider "virtualbox" do |vb|
        vb.name = name
        vb.memory = cfg[:memory]
        vb.cpus = cfg[:cpus]
      end

      #
      # Provisioning
      #

      case name

      when "k8s-master"

        node.vm.synced_folder "./k8s-manifests",
                              "/k8s-manifests",
                              create: true

        node.vm.provision "shell",
          path: "provisioning/common.sh",
          env: { "NODE_IP" => cfg[:ip] }

        node.vm.provision "shell",
          path: "provisioning/master-init.sh",
          env: { "NODE_IP" => cfg[:ip] }

      when "k8s-worker1"

        node.vm.provision "shell",
          path: "provisioning/common.sh",
          env: { "NODE_IP" => cfg[:ip] }

        node.vm.provision "shell",
          path: "provisioning/worker-join.sh"

      when "buildserver"

        node.vm.synced_folder "./observability-platform",
                              "/observability-platform",
                              create: true

        node.vm.synced_folder "./cluster-stats",
                              "/cluster-stats",
                              create: true

        node.vm.synced_folder "./cluster-monitor",
                              "/cluster-monitor",
                              create: true

        node.vm.synced_folder "./gateway",
                              "/gateway",
                              create: true

        node.vm.provision "shell",
          path: "provisioning/buildserver.sh"

      when "monitoring"

        node.vm.provision "shell",
          path: "provisioning/monitoring.sh"

      when "llm"

        node.vm.synced_folder "./ai-agent",
                              "/ai-agent",
                              create: true

        node.vm.provision "shell",
          path: "provisioning/llm.sh"

      end

    end

  end

  # After any `vagrant up`, pull the freshly-generated admin kubeconfig
  # out of the shared folder onto the host and label the worker node -
  # the two steps that used to be manual. No-op (harmless) if the
  # control plane isn't up yet, e.g. when only bringing up buildserver.
  config.trigger.after :up do |trigger|
    trigger.info = "Syncing kubeconfig to host and labeling worker node..."
    trigger.ruby do
      require "fileutils"
      admin_conf = File.join(__dir__, "shared_folder", "admin.conf")
      next unless File.exist?(admin_conf)

      kube_dir = File.expand_path("~/.kube")
      FileUtils.mkdir_p(kube_dir)
      FileUtils.cp(admin_conf, File.join(kube_dir, "config"))
      File.chmod(0600, File.join(kube_dir, "config"))
      puts "kubeconfig installed at ~/.kube/config"

      system("kubectl label node k8s-worker1 node-role.kubernetes.io/worker=worker --overwrite >/dev/null 2>&1")
    end
  end

end
