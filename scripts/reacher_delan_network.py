import numpy as np
import matplotlib.pyplot as plt
from tqdm import tqdm # Displays a progress bar

import torch
from torch import nn
from torch import optim
import torch.nn.functional as F
from torchvision import datasets, transforms
from torch.utils.data import TensorDataset, Dataset, Subset, DataLoader, random_split
import random
from dataset import TrajectoryDataset
# torch.manual_seed(0) # Fix random seed for reproducibility

def generate_train_test_indices(data, num_train_chars=1, num_samples_per_char=1):
    char_count = {}
    char_indices = {}
    train_chars = []
    test_chars = []
    train_trajectories = []
    test_trajectories = []

    for i, label in enumerate(data['labels']):
        idx = label[0]
        letter = data['keys'][idx-1][0]
        if letter in char_count:
            char_count[letter] += 1
            char_indices[letter].append(i)

        else:
            test_chars.append(letter)
            char_count[letter] = 1
            char_indices[letter] = [i]

    for i in range(num_train_chars):
        if len(test_chars) > 0:
            train_char_idx = random.randint(0,len(test_chars)-1)
            train_char = test_chars.pop(train_char_idx)
            train_chars.append(train_char)
            if num_samples_per_char < len(char_indices[train_char]):
                train_trajectories += char_indices[train_char][:num_samples_per_char]
            else:
                train_trajectories += char_indices[train_char]

    for test_char in test_chars:
            if num_samples_per_char < len(char_indices[test_char]):
                test_trajectories += char_indices[test_char][:num_samples_per_char]
            else:
                test_trajectories += char_indices[test_char]

    return train_trajectories, test_trajectories

class Reacher_DeLaN_Network(nn.Module):
    def __init__(self):
        super().__init__()
        # TODO: Design your own network, define layers here.
        input_dim = 2
        h1_dim = 6
        h2_dim = 6
        # joint angle input layer
        self.fc1 = nn.Linear(input_dim, h1_dim)
        # torch.nn.init.zeros_(self.fc1.weight)
        # torch.nn.init.zeros_(self.fc1.bias)

        # 1st hidden layer
        self.fc1a = nn.Linear(h1_dim, h2_dim)
        # torch.nn.init.zeros_(self.fc1a.weight)
        # torch.nn.init.zeros_(self.fc1a.bias)

        # gravity layer
        self.fc2 = nn.Linear(h2_dim, input_dim) 
        # torch.nn.init.zeros_(self.fc2.weight)
        # torch.nn.init.zeros_(self.fc2.bias)

        # ld layer
        self.fc3 = nn.Linear(h2_dim, input_dim)
        # torch.nn.init.ones_(self.fc3.weight)
        # torch.nn.init.ones_(self.fc3.bias)

        # lo layer
        self.fc4 = nn.Linear(h2_dim, 1)
        # torch.nn.init.zeros_(self.fc4.weight)
        # torch.nn.init.zeros_(self.fc4.bias)

    def forward(self,x):
        d = x.shape[1] // 3
        n = x.shape[0]
        q, q_dot, q_ddot = torch.split(x,[d,d,d], dim = 1)

        # q.requires_grad = True
        h1 = F.relu(self.fc1(q))
        h2 = F.relu(self.fc1a(h1))
        
        # Gravity torque
        g = self.fc2(h2)

        # ld is vector of diagonal L terms, lo is vector of off-diagonal L terms
        ld = F.relu(self.fc3(h2))
        lo = self.fc4(h2)

        dRelu_fc1 = torch.where(h1 > 0, torch.ones(h1.shape), torch.zeros(h1.shape))
        dh1_dq = torch.diag_embed(dRelu_fc1) @ self.fc1.weight

        dRelu_fc1a = torch.where(h2 > 0, torch.ones(h2.shape), torch.zeros(h2.shape))
        dh2_dh1 = torch.diag_embed(dRelu_fc1a) @ self.fc1a.weight

        dRelu_fc3 = torch.where(ld > 0, torch.ones(ld.shape), torch.zeros(ld.shape))
        dld_dh2 = torch.diag_embed(dRelu_fc3) @ self.fc3.weight
        dlo_dh2 = self.fc4.weight
        
        dld_dq = dld_dh2 @ dh2_dh1 @ dh1_dq
        dlo_dq = dlo_dh2 @ dh2_dh1 @ dh1_dq
        dld_dqi = dld_dq.permute(0,2,1).view(n,d,d,1)
        dlo_dqi = dlo_dq.permute(0,2,1).view(n,d,1,1)

        # dl_dq = torch.cat([dld_dq,dlo_dq],dim=1)

        dld_dt = dld_dq @ q_dot.view(n,d,1)
        dlo_dt = dlo_dq @ q_dot.view(n,d,1)

        dL_dt = torch.tril(torch.ones(n,d,d)) - torch.eye(d)
        dL_dqi = torch.tril(torch.ones(n,d,d,d)) - torch.eye(d)

        L = torch.tril(torch.ones(n,d,d)) - torch.eye(d)

        indices = dL_dt == 1
        indices_dL_dqi = dL_dqi == 1

        dL_dt[indices] = dlo_dt.view(n)
        L[indices] = lo.view(n)
        dL_dqi[indices_dL_dqi] = dlo_dqi.view(n*d)

        dL_dt += torch.diag_embed(dld_dt.view(n,d))
        L += torch.diag_embed(ld.view(n,d))
        dL_dqi += torch.diag_embed(dld_dqi.view(n,d,d))

        # Mass Matrix
        epsilon = .00001    #small number to ensure positive definiteness of H

        H = L.permute(0,2,1) @ L + epsilon * torch.eye(d)

        # Time derivative of Mass Matrix
        dH_dt = L @ dL_dt.permute(0,2,1) + dL_dt @ L.permute(0,2,1)

        quadratic_term = q_dot.view(n,1,1,d) @ (dL_dqi @ L.permute(0,2,1).view(n,1,d,d) + L.view(n,1,d,d) @ dL_dqi.permute(0,1,3,2)) @ q_dot.view(n,1,d,1)
        c = dH_dt @ q_dot.view(n,d,1) + quadratic_term.view(n,d,1)

        tau =  H @ q_ddot.view(n,d,1) + c + g.view(n,d,1)

        # The loss layer will be applied outside Network class
        return (tau.squeeze(), H.squeeze(), c.squeeze(), g.squeeze())

def train(model, loader, num_epoch = 10): # Train the model
    print("Start training...")
    model.train() # Set the model to training mode
    for i in range(num_epoch):
        running_loss = []
        for batch, label in tqdm(loader):
            batch = batch.to(device)
            label = label.to(device)
            optimizer.zero_grad() # Clear gradients from the previous iteration
            pred_tau, pred_H, pred_c, pred_g = model(batch) # This will call Network.forward() that you implement
            loss = criterion(pred_tau, label) # Calculate the loss
            running_loss.append(loss.item())
            loss.backward() # Backprop gradients to all tensors in the network
            optimizer.step() # Update trainable weights
        print("Epoch {} loss:{}".format(i+1,np.mean(running_loss))) # Print the average loss for this epoch
    
    print("Done!")

def evaluate(model, loader): # Evaluate accuracy on validation / test set
    model.eval() # Set the model to evaluation mode
    MSEs = []
    with torch.no_grad(): # Do not calculate grident to speed up computation
        for batch, label in tqdm(loader):
            batch = batch.to(device)
            label = label.to(device)
            pred_tau, pred_H, pred_c, pred_g = model(batch)
            MSE_error = criterion(pred_tau, label)
            MSEs.append(MSE_error.item())
            # fig, axs = plt.subplots(2, sharex=True)
            # axs[0].plot(label[:,0],label='Calculated',color='b')
            # axs[0].plot(pred[:,0],label='Predicted',color='r')
            # axs[0].legend()
            # axs[0].set_ylabel(r'$\tau_1\,(N-m)$')
            # axs[1].plot(label[:,1],label='Calculated',color='b')
            # axs[1].plot(pred[:,1],label='Predicted',color='r')
            # axs[1].legend()
            # axs[1].set_xlabel('Time Step')
            # axs[1].set_ylabel(r'$\tau_2\,(N-m)$')
            # fig.suptitle('DeLaN Network')
            # plt.show()
            # plt.close()
    Ave_MSE = np.mean(np.array(MSEs))
    print("Average Evaluation MSE: {}".format(Ave_MSE))
    return Ave_MSE

if __name__ == '__main__':
    # Load the dataset and train and test splits
    print("Loading dataset...")
    data = np.load('../data/trajectories_joint_space.npz', allow_pickle=True)
    train_trajectories, test_trajectories = generate_train_test_indices(data, num_train_chars=2, num_samples_per_char=2)
    TRAJ_train = TrajectoryDataset(data,train_trajectories)
    TRAJ_test = TrajectoryDataset(data,test_trajectories)
    print("Done!")
    trainloader = DataLoader(TRAJ_train, batch_size=None)
    testloader = DataLoader(TRAJ_test, batch_size=None)

    # create model and specify hyperparameters
    device = "cuda" if torch.cuda.is_available() else "cpu" # Configure device
    model = Reacher_DeLaN_Network().to(device)
    criterion = nn.MSELoss() # Specify the loss layer
    # TODO: Modify the line below, experiment with different optimizers and parameters (such as learning rate)
    optimizer = optim.Adam(model.parameters(), lr=5e-2, weight_decay=1e-4) # Specify optimizer and assign trainable parameters to it, weight_decay is L2 regularization strength
    num_epoch = 50 # TODO: Choose an appropriate number of training epochs

    # train and evaluate network
    train(model, trainloader, num_epoch)
    evaluate(model, testloader)