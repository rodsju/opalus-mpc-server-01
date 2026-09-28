@NonCPS
def cancelPreviousBuilds() {
  String jobname = env.JOB_NAME
  int currentBuildNum = env.BUILD_NUMBER.toInteger()

  def job = Jenkins.instance.getItemByFullName(jobname)
  for (build in job.builds) {
    if (build.isBuilding() && currentBuildNum > build.getNumber().toInteger()) {
      build.doStop();
      echo "Build ${build.toString()} cancelled"
    }
  }
}

def githubNotify(state, context, git_commit) {
}

// Função parametrizada para deploy em diferentes ambientes
def deployToEnvironment(String environment) {
    // Configuração SSH
    def sshCredentialId = 'SSH_HYPERCC_1'
    def sshServer = '38.211.131.26'
    def sshUser = 'ubuntu'

    // Conexão SSH usando withCredentials em vez de sshagent
    withCredentials([sshUserPrivateKey(credentialsId: sshCredentialId, keyFileVariable: 'SSH_KEY')]) {
        sh """
            ssh -o StrictHostKeyChecking=no -i \$SSH_KEY ${sshUser}@${sshServer} 'echo Conectado ao servidor ${environment}'
            ssh -o StrictHostKeyChecking=no -i \$SSH_KEY ${sshUser}@${sshServer} 'cd ~/apps/vert-mcp-server/ && bash deploy-production.sh'
        """
    }
}

pipeline {
    agent {
        docker {
            image "akaytatsu/cibuilder:latest"
        }
    }

    stages {

        stage('Init') {
            steps {
                cancelPreviousBuilds()
                script {
                    githubNotify('INPROGRESS', env.BRANCH_NAME, env.GIT_COMMIT)
                }
            }
        }

        // stage('Code Checkout') {
        //     steps {
        //         checkout scm
        //     }
        // }

        // stage('Build Docker Images') {
        //     steps {
        //         script {
        //             sh 'docker-compose -f docker-compose.yml -f docker-compose.tests.yml down'
        //             sh 'docker-compose -f docker-compose.yml -f docker-compose.tests.yml build'
        //             sh 'docker-compose -f docker-compose.yml -f docker-compose.tests.yml up -d --no-build'
        //         }
        //     }
        // }

        // stage('stop containers') {
        //     steps {
        //         script {
        //             sh 'docker-compose -f docker-compose.yml -f docker-compose.tests.yml down'
        //         }
        //     }
        // }

        stage('Build and Deploy') {
            when {
                expression {
                    return env.GIT_BRANCH == 'main'
                }
            }
            steps {
                script {
                    // Deploy para o ambiente apropriado
                    deployToEnvironment(env.GIT_BRANCH)
                }
            }
        }

    }

    // post {
    //     always {
    //         echo "Stop Docker image"
    //         script{
    //             sh 'docker-compose -f docker-compose.yml -f docker-compose.tests.yml down'
    //         }
    //     }

    //     success {
    //         echo "Notify bitbucket success"
    //         script {
    //             sh 'docker-compose -f docker-compose.yml -f docker-compose.tests.yml down'
    //             githubNotify('SUCCESSFUL', env.BRANCH_NAME, env.GIT_COMMIT)
    //         }
    //     }

    //     failure {
    //         echo "Notify bitbucket failure"
    //         script {
    //             sh 'docker-compose -f docker-compose.yml -f docker-compose.tests.yml down'
    //             githubNotify('FAILED', env.BRANCH_NAME, env.GIT_COMMIT)
    //         }
    //     }

    //     aborted {
    //         echo "Notify bitbucket failure"
    //         script {
    //             sh 'docker-compose -f docker-compose.yml -f docker-compose.tests.yml down'
    //             githubNotify('FAILED', env.BRANCH_NAME, env.GIT_COMMIT)
    //         }
    //     }

    //     unsuccessful {
    //         echo "Notify bitbucket failure"
    //         script {
    //             sh 'docker-compose -f docker-compose.yml -f docker-compose.tests.yml down'
    //             githubNotify('FAILED', env.BRANCH_NAME, env.GIT_COMMIT)
    //         }
    //     }
    // }
}
