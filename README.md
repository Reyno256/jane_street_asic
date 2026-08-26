## Intro 
I am a CS undergrad(est. graduation may 2027) with some prior ASIC experience(on the compiler side). Of note is that the UofT( my school) computer science program only has one class which focuses on actual silicon and in that course all of our designs(to be simulated on an FPGA) had to be created in a program called Logisim Evolution. While this is a great tool for learning, it means I understand what the goal of verilog programs are but I can't read them. That background colours my approach to this problem. 
## Warmup
The warmup portion of this project is essentially one labelled training example for reconstructing verilog from a gds file. As a CS student, to me this looks(at least conceptually) like a decompilation problem. Seeing it from that lens, I need to make sure that I have all the dependencies for the project so I can work backwards. Below is a diagram to explain my thought process at this point. 
![Warmup reconstruction process](<Writeup Images/jane street.drawio.png>)

Components in blue are what we are trying to build, green is what we have and red are missing/unknown. 

The problem statement suggests that "the design uses an open-source PDK" but I wasn't sure what that was. At this point I decided that the best course of action was to use the warmup GDS and Netlist to try to recreate the portion of the PDK that I needed to make sense of the puzzle GDS.  The file in question was the LEF(Library Exchange Format), which basically interprets each piece of the GDS. Since Cadence and Synopsis are expensive I was limited to open source tools. Luckily, there is an open source tool called KLayout which works well here. 

Here we see the first real hint that the warmup files give us, in the warmup file 01_netlist.v, many lines reference sky130  eg "sky130_fd_sc_hd__decap_3 PHY_EDGE_ROW_0_Left_29 ();". This is an open source PDK which totally sidesteps KLayout and extracting the LEF. Now we can download all the files related to this chip process. Our chart now has a little more green.
![Warmup reconstruction process with the Sky130 PDK](<Writeup Images/jane street.drawio(1).png>)

The blog is extremely helpful to us here as it suggests this open source repo https://github.com/rtimothyedwards/magic for understanding the chip structure. Magic has a perfect tool for us in this case. In fact it is a relatively direct application of the tool to reconstruct a SPICE Netlist using the PDK and GDS. Again, we should update our chart.

![Warmup reconstruction process with Magic and Netgen](<Writeup Images/jane street.drawio(2).png>)
Now for the fun part, reconstructing the Verilog and figuring out how to read it. 
We have two separate but related problems:
- Reconstructing the Verilog
	- The author of the Magic repo also has a compatible tool for this step called Netgen 
- Reading the Verilog
	- While I can't read Verilog directly, I can certainly take a look at a circuit as a whole and thankfully there are tools for my very situation. For this project I used https://digitaljs.tilk.eu/#. 
Unfortunately, Netgen struggled with generating canonical Verilog which my tool could read. Thankfully, Claude is here to help, it was able to write a simple translation between the SPICE and Verilog. Below is the canonical 01_netlist.v from the warmup file viewed through https://digitaljs.tilk.eu/# and then after that is the final output of my Claude generated script. 

![Canonical warmup netlist](<Writeup Images/Screenshot 2026-08-26 at 11.41.56 AM.png>)

![Recovered warmup netlist](<Writeup Images/Screenshot 2026-08-26 at 11.36.02 AM.png>)

This image, however is not the result of raw translation as it has these, very easy to read large blocks. These blocks are the result of a second script, this time one that performs gate-level structural recovery using graph traversal on D-flip flop chains where outputs are brute forced against the truth tables for known components. Now we have a process which gives us a full green chart. 
![Completed warmup reconstruction process](<Writeup Images/jane street.drawio(3).png>)
Now, time to move on to the real puzzle. We know that there is a bunch of the chip designed to produce a specific output when we get the success condition, so for now we can ignore that chunk.
## Puzzle
Once we run the pipeline we built for the warmup, we have a full SPICE Netlist for the chip and can simulate it. At this point we can actually solve the final answer, without ever moving up the chart to the Verilog. The simplest way to get the correct input is just to use an SAT solver, like the one built into the underlying simulation code for https://digitaljs.tilk.eu/#, Yosys. While anticlimactic, this gives us the final answer and lets us find out what the correct output is. 

The answer to the problem is 0000000101010000100000000000001010101000000000000101000000100000100000010000010100001000000010000001000001001000101000000000 or PPA@ once converted to ASCII(with two trailing zeros). Then we get what to me was the most real confirmation of our having solved the problem which was an output of 42(when reading Out in parallel one cycle after success). This looked to me like it was a Hitchiker's Guide to the Galaxy reference(the answer to life the universe and everything is 42).  Immediately upon success out is actually 40, which forced me to reconsider whether it was a reference. It turns out I was incorrect and if you keep reading out, it becomes 

(\* TWO STARS \*)

which is a reference to a solution found by Jane Street to a different game. (https://www.janestreet.com/culture/street-view/numberphile-star-puzzle/)

Now that we have the answer to the problem, lets dig a little further into the chip. A first pass through our existing process renders this, basically unreadable and giant. 
![Puzzle circuit](<Writeup Images/Screenshot 2026-08-26 at 12.36.39 PM.png>)

While interpreting this is challenging it basically looks like a large finite state machine looking for the correct input. The massive central block is the combinatorial portion and everything else are just flip flops. 
## Author 
My name is Quinn Reynolds and I am a 4th year undergrad at the University of Toronto, with experience across the ML stack but specifically in hardware verification and ML compilers. https://www.linkedin.com/in/qreynolds/