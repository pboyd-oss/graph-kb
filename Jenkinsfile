@Library('jenkins-library') _

pipeline {
    agent {
        kubernetes {
            cloud env.TUXGRID_BUILD_CLOUD
            inheritFrom 'platform-builder'
            yaml '''
spec:
  containers:
    - name: deploy-sec-base
      image: harbor.tuxgrid.com/platform/deploy-sec-base:latest
      command: ["cat"]
      tty: true
      resources:
        requests:
          cpu: 100m
          memory: 256Mi
        limits:
          cpu: "1"
          memory: 1Gi
'''
        }
    }

    environment {
        IMAGE = 'harbor.tuxgrid.com/platform/graph-kb'
    }

    options {
        timeout(time: 30, unit: 'MINUTES')
        buildDiscarder(logRotator(numToKeepStr: '20'))
    }

    triggers {
        pollSCM('H/5 * * * *')
    }

    stages {
        stage('Checkout') {
            steps { checkout scm }
        }

        stage('Build') {
            steps {
                script { platformBuild() }
            }
        }

        stage('Archive') {
            steps {
                script { platformArchive() }
            }
        }

        stage('Sign') {
            steps {
                script { platformSign() }
            }
        }

        stage('Provenance') {
            steps {
                script { platformBuildProvenance() }
            }
        }

        stage('Deploy') {
            when { branch 'main' }
            steps {
                container('deploy-sec-base') {
                    sh '''
                        export KUBECONFIG="${WORKSPACE}/.kubeconfig"
                        kubectl config set-cluster in-cluster \
                            --server=https://kubernetes.default.svc \
                            --certificate-authority=/var/run/secrets/kubernetes.io/serviceaccount/ca.crt \
                            --embed-certs=true
                        kubectl config set-credentials sa \
                            --token="$(cat /var/run/secrets/kubernetes.io/serviceaccount/token)"
                        kubectl config set-context in-cluster --cluster=in-cluster --user=sa
                        kubectl config use-context in-cluster
                        skaffold render --build-artifacts=artifacts.json --output=rendered.yaml
                        skaffold apply rendered.yaml
                    '''
                }
            }
        }
    }
}
