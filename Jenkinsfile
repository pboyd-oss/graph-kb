@Library('jenkins-library') _

pipeline {
    agent {
        kubernetes {
            cloud env.TUXGRID_BUILD_CLOUD
            inheritFrom 'skaffold'
        }
    }

    stages {
        stage('Checkout') {
            steps { checkout scm }
        }

        stage('Build') {
            steps {
                script { buildApp() }
            }
        }

        stage('Deploy') {
            when { branch 'main' }
            steps {
                container('skaffold') {
                    sh 'skaffold deploy --build-artifacts=artifacts.json --namespace=graph-kb'
                }
            }
        }
    }
}
